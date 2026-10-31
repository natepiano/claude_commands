"""Style planner borrowing contracts."""

from __future__ import annotations

import io
import re
import sqlite3
import sys
import unittest
from collections.abc import Mapping
from contextlib import closing, redirect_stdout
from pathlib import Path
from typing import final
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import style_plan
import berth_board
import unit_plans
import style_proposals
import rules
import style_ledger

from style_plan_test_support import StylePlanFixture


@final
class StylePlanTests(StylePlanFixture):
    def borrowed_proposal_ticket(self) -> tuple[style_ledger.Ledger, Path, style_ledger.TicketRecord]:
        """Refill lends names-unit an approved values decision without rewriting the approval."""
        values = self.borrowing_family("values")
        self.sync()
        for path in self.sources:
            self.record(values, path, self.tip)
        issue = self.borrowing_decision(values, "values", self.sources[0])
        approval = issue.read_bytes()
        with mock.patch.object(style_proposals, "proposal_command"):
            self.assertEqual(self.run_next()[0], 0)
        ledger = self.ledger()
        ticket, = ledger.ticket_open("names-unit")
        self.assertEqual((ticket["family"], ticket["proposal"]), ("values", str(issue)))
        self.assertEqual(issue.read_bytes(), approval)
        return ledger, issue, ticket

    def test_borrowed_proposal_refuses_approval_for_an_unrelated_rule(self) -> None:
        _, issue, ticket = self.borrowed_proposal_ticket()
        _ = issue.write_text(issue.read_text().replace(f"style_rule: {ticket['rules'][0]}",
                                                     "style_rule: global:rust/unrelated.md"))
        self.assertEqual(self.reference_gate(ticket["id"]),
                         (1, "review: proposal approval or filed findings changed\n"))

    def test_borrowed_proposal_refuses_approval_for_an_unrelated_crate(self) -> None:
        _, issue, ticket = self.borrowed_proposal_ticket()
        _ = issue.write_text(issue.read_text().replace("style_crate: crates/alpha", "style_crate: crates/beta"))
        self.assertEqual(self.reference_gate(ticket["id"]),
                         (1, "review: proposal approval or filed findings changed\n"))

    def test_borrowed_proposal_keeps_its_approval_through_gate_checkpoint_and_merge(self) -> None:
        ledger, issue, ticket = self.borrowed_proposal_ticket()
        approval = issue.read_bytes()
        self.assertEqual(self.reference_gate(ticket["id"]), (0, "approved proposal\n"))
        # A moved finding is still refused, regardless of which unit took the approved work.
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
        self.assertEqual(ledger.ticket_open("names-unit")[0]["state"], "checkpointed")
        ledger.set_tip(checkpoint, "main")
        _ = ledger.ticket_merge(ticket["id"])
        self.assertEqual(ledger.ticket_open("names-unit"), ())
        self.assertEqual(issue.read_bytes(), approval)

    def borrowing_family(self, family: rules.Family) -> str:
        worktree = self.root / family
        _ = self.git("worktree", "add", "-q", "-b", f"demo-style-{family}", str(worktree))
        plan = worktree / f"docs/style-production/units/{family}-unit.md"
        plan.parent.mkdir(parents=True)
        _ = plan.write_text(self.plan_header().replace("names-unit", family + "-unit"))
        _ = self.doc.write_text(self.doc.read_text() +
                                f"| {family}-unit | docs/style-production/units/{family}-unit.md | {worktree} | demo-style-{family} |\n")
        return self.rule(family + "-borrow", f"tier: C\nfamily: {family}\ninput_scope: file\n")

    def assert_projected_follow_count_matches_borrow(self) -> None:
        values = self.borrowing_family("values")
        source, destination = self.sources[:2]
        self.commit_files({source: "pub struct Parent {\n    old: u32,\n}\n"})
        issue, (finding,) = self.judged_findings(values, ((source, 2, "old: u32,"),))
        _ = issue.write_text(issue.read_text().replace("unit: names-unit", "unit: values-unit"))
        self.write_generated_body(issue)
        _ = self.git("rm", "-q", source)
        self.commit_files({destination: "pub struct Parent {\n    old: u32,\n}\n"})
        self.finish_judging()
        ledger = self.ledger()
        before = tuple(ledger._connection.iterdump())  # pyright: ignore[reportPrivateUsage]
        busy = style_plan.BusyFiles(self.board(source), frozenset())
        definitions = {rule.rule_id: rule for rule in rules.load_rules(self.repo)}
        with redirect_stdout(io.StringIO()):
            count = style_plan._select_phase(  # pyright: ignore[reportPrivateUsage]
                ledger, "names-unit", "demo-style-names", self.plan, self.repo, self.doc, self.plan.read_text(),
                unit_plans.AllCrates(), busy, frozenset(), definitions, style_plan.MainChanges(self.tip),
                family="values", selection=style_plan.FamilySelection.COUNT)
        self.assertIsInstance(count, style_plan.BuildableFindings)
        assert isinstance(count, style_plan.BuildableFindings)
        self.assertEqual(count.count, 1)
        self.assertEqual(tuple(ledger._connection.iterdump()), before)  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(ledger.named_held((finding.finding_id,))[0].path, source)
        output = io.StringIO()
        with (mock.patch.object(style_proposals, "proposal_command"),
              mock.patch.object(style_proposals, "github_rule_root",
                                return_value=style_proposals.GitHubRuleRoot("example/nate_style", "main")),
              redirect_stdout(output)):
            result = style_plan.select_family_work(
                ledger, "names-unit", "demo-style-names", self.plan, self.repo, self.doc, self.plan.read_text(),
                unit_plans.AllCrates(), busy, frozenset(), definitions, style_plan.MainChanges(self.tip),
                style_plan.NoCheckpointBlock())
        self.assertIsInstance(result, style_plan.PhaseWritten, output.getvalue())
        ticket, = ledger.ticket_open("names-unit")
        self.assertEqual((ticket["family"], ticket["files"], ticket["finding_ids"]),
                         ("values", [destination], [finding.finding_id]))

    def test_borrowing_sync_wait_chooses_most_buildable_and_pins_real_family(self) -> None:
        own = self.rule("read")
        values = self.borrowing_family("values")
        types = self.borrowing_family("types")
        self.sync()
        for path in self.sources:
            self.record(types, path, self.tip)
            self.record(values, path, self.tip)
        _ = self.borrowing_decision(values, "values", self.sources[2], 3)
        _ = self.borrowing_decision(types, "types", self.sources[3], 1)
        self.write(self.sources[0], "pub fn incoming() {}\n")
        _ = self.commit()
        with (mock.patch.object(style_proposals, "proposal_command"),
              mock.patch.object(style_plan, "ticket_sync_wait", wraps=style_plan.ticket_sync_wait) as sync):
            code, _, errors = self.run_next()
        self.assertEqual((code, errors), (0, ""))
        ticket, = self.ledger().ticket_open("names-unit")
        self.assertEqual((ticket["family"], ticket["rules"]), ("values", [values]))
        self.assertTrue(any(call.args[2] == "read" for call in sync.call_args_list))
        text = self.plan.read_text()
        self.assertIn("Borrowed from values: names has no buildable work (waiting for sync", text)
        self.assertNotIn(f"- `{own}`:", text)
        pinned, = self.ledger().pinned_rule_texts(ticket["id"])
        self.assertEqual(pinned.rule_id, values)
        self.assertIn(pinned.blob, text)
        # Resume preserves the borrowing explanation and the borrowed rule pin.
        self.assertEqual(self.run_next()[0], 0)
        self.assertEqual(self.plan.read_text(), text)

    def test_borrowing_own_buildable_ticket_never_counts_other_families(self) -> None:
        _ = self.rule("read")
        values = self.borrowing_family("values")
        self.sync()
        for path in self.sources:
            self.record(values, path, self.tip)
        _ = self.borrowing_decision(values, "values", self.sources[2], 5)
        with mock.patch.object(style_plan, "family_plan_settings", wraps=style_plan.family_plan_settings) as settings:
            self.assertEqual(self.run_next()[0], 0)
        settings.assert_not_called()
        self.assertEqual(self.ledger().ticket_open("names-unit")[0]["family"], "names")
        self.assertNotIn("Borrowed from", self.plan.read_text())

    def test_borrowing_overlap_tries_the_next_family(self) -> None:
        values = self.borrowing_family("values")
        types = self.borrowing_family("types")
        self.sync()
        for path in self.sources:
            self.record(types, path, self.tip)
        _ = self.borrowing_decision(types, "types", self.sources[0], 4)
        _ = self.borrowing_decision(values, "values", self.sources[2], 1)
        self.finish_judging()
        predecessor = style_plan.WaitingCheckpoint("names-unit-1", self.tip, frozenset({self.sources[0]}),
                                                    ("crates/alpha", "crates/beta"))
        with (mock.patch.object(style_proposals, "proposal_command"),
              style_ledger.open_ledger(self.repo) as ledger):
            selected = style_plan.select_family_work(
                ledger, "names-unit", "demo-style-names", self.plan, self.repo, self.doc, self.plan.read_text(),
                unit_plans.AllCrates(), style_plan.BusyFiles(self.board(), frozenset()), frozenset(),
                {rule.rule_id: rule for rule in rules.load_rules(self.repo)}, style_plan.MainChanges(self.tip), predecessor)
        self.assertIsInstance(selected, style_plan.PhaseWritten)
        self.assertEqual(self.ledger().ticket_open("names-unit")[0]["family"], "values")

    def test_borrowing_rank_holds_a_lower_family_on_the_same_file(self) -> None:
        values = self.borrowing_family("values")
        _ = self.borrowing_family("types")
        self.sync()
        _ = self.borrowing_decision(values, "values", self.sources[0], 4)
        with mock.patch.object(style_proposals, "proposal_command"):
            code, _, errors = self.run_next()
        self.assertEqual((code, errors), (3, ""))
        self.assertEqual(self.ledger().ticket_open("names-unit"), ())

    def test_borrowing_idle_reports_every_family_and_keeps_own_exit_code(self) -> None:
        _ = self.rule("read")
        _ = self.rule("structure", "tier: D\nfamily: modules\n")
        self.sync()
        output = io.StringIO()
        with (redirect_stdout(output), mock.patch.object(berth_board, "read_board", return_value=self.board(*self.sources))):
            code = style_plan.main(["next-phase", "--unit", "names-unit", "--plan", str(self.plan), "--repo", str(self.repo)])
        self.assertEqual(code, 3)
        self.assertIn("structure stays with modules:", output.getvalue())
        self.assertIn("file busy", output.getvalue())
        self.assertIn("no available work:", output.getvalue())
        diagnostic = next(line for line in output.getvalue().splitlines() if line.startswith("no available work:"))
        for family in style_plan.FAMILIES:
            self.assertIn(family + " ", diagnostic)
        self.assertIn("names waiting for structure", diagnostic)
        self.assertIn("waiting for structure: crates/alpha, crates/beta", output.getvalue())

    def test_borrowing_idle_diagnostic_keeps_control_characters_on_one_line(self) -> None:
        result = style_plan.WaitingForSync(0, families=(style_plan.FamilyIdleReason("names", "waiting for sync: line\rbreak.rs"),))
        output = io.StringIO()
        with (mock.patch.object(style_plan, "next_phase", return_value=result),
              mock.patch.object(style_ledger, "record_refill"), redirect_stdout(output)):
            code = style_plan.main(["next-phase", "--unit", "names-unit", "--plan", str(self.plan), "--repo", str(self.repo)])
        self.assertEqual((code, output.getvalue()), (3, "no available work: names waiting for sync: line break.rs\nwaiting for sync\n"))

    def test_borrowing_count_is_read_only_and_excludes_stale_and_held_findings(self) -> None:
        values = self.borrowing_family("values")
        self.sync()
        _ = self.borrowing_decision(values, "values", self.sources[0], 2)
        self.finish_judging()
        ledger = self.ledger()
        before = tuple(ledger._connection.iterdump())  # pyright: ignore[reportPrivateUsage]
        with (mock.patch.object(style_proposals, "proposal_command", side_effect=AssertionError("preview writes issue")),
              redirect_stdout(io.StringIO())):
            result = style_plan._select_phase(  # pyright: ignore[reportPrivateUsage]
                ledger, "names-unit", "demo-style-names", self.plan, self.repo, self.doc, self.plan.read_text(),
                unit_plans.AllCrates(), style_plan.BusyFiles(self.board(), frozenset()), frozenset(),
                {rule.rule_id: rule for rule in rules.load_rules(self.repo)}, style_plan.MainChanges(self.tip),
                family="values", selection=style_plan.FamilySelection.COUNT)
        self.assertEqual(result, style_plan.BuildableFindings(2, "none approved"))
        self.assertEqual(tuple(ledger._connection.iterdump()), before)  # pyright: ignore[reportPrivateUsage]
        issue = self.root / "borrow-issues/values.md"
        findings = ledger.proposal_findings(issue)
        ledger.ticket_add("values-unit-1", "values-unit", "proposal", "values", "crates/alpha", [self.sources[0]], [values],
                          proposal=str(issue), finding_ids=tuple(finding.finding_id for finding in findings))
        with (mock.patch.object(ledger, "cells", side_effect=AssertionError("held family scans coverage")),
              redirect_stdout(io.StringIO())):
            held = style_plan._select_phase(  # pyright: ignore[reportPrivateUsage]
                ledger, "names-unit", "demo-style-names", self.plan, self.repo, self.doc, self.plan.read_text(),
                unit_plans.AllCrates(), style_plan.BusyFiles(self.board(), frozenset()), frozenset(),
                {rule.rule_id: rule for rule in rules.load_rules(self.repo)}, style_plan.MainChanges(self.tip),
                family="values", selection=style_plan.FamilySelection.COUNT)
        self.assertEqual(held, style_plan.BuildableFindings(0, "held by build ticket (2)"))
        ledger.ticket_drop("values-unit-1")
        _ = (self.rule_dir / "values-borrow.md").write_text((self.rule_dir / "values-borrow.md").read_text() + "New body.\n")
        with (mock.patch.object(ledger, "cells", wraps=ledger.cells) as scanned,
              redirect_stdout(io.StringIO())):
            stale = style_plan._select_phase(  # pyright: ignore[reportPrivateUsage]
                ledger, "names-unit", "demo-style-names", self.plan, self.repo, self.doc, self.plan.read_text(),
                unit_plans.AllCrates(), style_plan.BusyFiles(self.board(), frozenset()), frozenset(),
                {rule.rule_id: rule for rule in rules.load_rules(self.repo)}, style_plan.MainChanges(self.tip),
                family="values", selection=style_plan.FamilySelection.COUNT)
        self.assertEqual(stale, style_plan.BuildableFindings(0, "all stale (2)"))
        scanned.assert_not_called()
        with mock.patch.object(style_proposals, "proposal_command"):
            code, _, _ = self.run_next()
        self.assertEqual(code, 3)
        self.assertEqual(ledger.ticket_open("names-unit"), ())

    def test_borrowing_mixed_current_and_stale_findings_stay_filtered_after_follow(self) -> None:
        values = self.borrowing_family("values")
        self.sync()
        issue = self.borrowing_decision(values, "values", self.sources[0], 2)
        ledger = self.ledger()
        stale = ledger.proposal_findings(issue)[0].finding_id

        def changed(finding: style_ledger.HeldFinding, _definitions: Mapping[str, rules.Rule]) -> bool:
            return finding.finding_id == stale

        self.finish_judging()
        with (mock.patch.object(style_ledger, "rule_changed_since_judged", side_effect=changed),
              mock.patch.object(style_proposals, "proposal_command"), redirect_stdout(io.StringIO())):
            count = style_plan._select_phase(  # pyright: ignore[reportPrivateUsage]
                ledger, "names-unit", "demo-style-names", self.plan, self.repo, self.doc, self.plan.read_text(),
                unit_plans.AllCrates(), style_plan.BusyFiles(self.board(), frozenset()), frozenset(),
                {rule.rule_id: rule for rule in rules.load_rules(self.repo)}, style_plan.MainChanges(self.tip),
                family="values", selection=style_plan.FamilySelection.COUNT)
            self.assertEqual(count, style_plan.BuildableFindings(1, "all stale (1)"))
            self.assertEqual(self.run_next()[0], 0)
        ticket, = ledger.ticket_open("names-unit")
        self.assertEqual(len(ticket["finding_ids"]), 1)
        self.assertNotIn(stale, ticket["finding_ids"])

    def test_borrowing_and_own_selection_share_full_open_ticket_footprints(self) -> None:
        values = self.borrowing_family("values")
        self.commit_files({self.sources[2]: "pub fn refer() { Outcome::Old; }\n"})
        issue = self.borrowing_decision(values, "values", self.sources[0])
        ledger = self.ledger()
        original, = ledger.proposal_findings(issue)
        blob = original.rule_blob
        rename = ledger.hold(values, self.sources[0], 1, "rename item", "rename",
                             "Outcome::Old", "Outcome::New", blob, "judge-values-1")
        ledger.file_held(rename.finding_id, issue)
        ledger.ticket_add("values-unit-1", "values-unit", "proposal", "values", "crates/alpha",
                          [self.sources[0]], [values], proposal=str(issue),
                          finding_ids=(original.finding_id, rename.finding_id))
        ledger.ticket_assign("values-unit-1", "demo-style-values")
        self.widen_for_pipeline("values-unit-1", self.sources[1])
        for path in (self.sources[1], self.sources[2]):
            finding = ledger.hold(values, path, 1, "candidate " + path, "type", "u32", "u64", blob, "judge-values-1")
            ledger.file_held(finding.finding_id, issue)
        self.finish_judging()
        busy = style_plan.busy_files(self.repo, self.doc, ledger, "names-unit", self.board())
        for path in (self.sources[0], self.sources[1], self.sources[2]):
            self.assertTrue(busy.contains(path), path)
        definitions = {rule.rule_id: rule for rule in rules.load_rules(self.repo)}
        # Both selector modes consume the same busy set, including the widened path
        # and a reference in a different crate, absent from the ticket's original files.
        for selection in (style_plan.FamilySelection.OWN, style_plan.FamilySelection.COUNT,
                          style_plan.FamilySelection.BORROW):
            with self.subTest(selection=selection), redirect_stdout(io.StringIO()):
                owner = "names-unit" if selection == style_plan.FamilySelection.OWN else "values-unit"
                _ = issue.write_text(re.sub(r"unit: .*", "unit: " + owner, issue.read_text()))
                result = style_plan._select_phase(  # pyright: ignore[reportPrivateUsage]
                    ledger, "names-unit", "demo-style-names", self.plan, self.repo, self.doc, self.plan.read_text(),
                    unit_plans.AllCrates(), busy, frozenset(), definitions, style_plan.MainChanges(self.tip),
                    family="values", selection=selection)
                if isinstance(result, style_plan.BuildableFindings):
                    self.assertEqual(result.count, 0)
                elif isinstance(result, style_plan.PhaseWritten):
                    ticket, = ledger.ticket_open("names-unit")
                    self.assertTrue(set(ticket["files"]).isdisjoint(self.sources[:3]))
                    ledger.ticket_drop(ticket["id"])
        self.assertEqual(ledger.ticket_open("names-unit"), ())
        # End-to-end borrowing also stays idle on those two unheld findings.
        with mock.patch.object(style_proposals, "proposal_command"):
            self.assertNotEqual(self.run_next()[0], 0)
        self.assertEqual(ledger.ticket_open("names-unit"), ())

    def test_borrowing_counts_projected_follow_locations_without_writing(self) -> None:
        self.assert_projected_follow_count_matches_borrow()

    def test_borrowing_projected_site_evidence_matches_borrow_with_higher_rank_unit(self) -> None:
        _ = self.types_unit()
        self.assert_projected_follow_count_matches_borrow()



if __name__ == "__main__":
    _ = unittest.main()
