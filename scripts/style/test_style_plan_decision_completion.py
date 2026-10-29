"""Decision completion, removed findings and queued-ticket recovery."""

from __future__ import annotations

import io
import sqlite3
import sys
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from typing import final
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import approved_renames
import style_plan
import unit_plans
import style_proposals
from issues import Issue, frontmatter

import rules
import style_ledger

from style_plan_test_support import StylePlanFixture


@final
class StylePlanTests(StylePlanFixture):
    def test_decision_refill_closes_a_decision_whose_only_finding_the_declaration_step_built(self) -> None:
        site = self.sources[0]
        parent = "pub struct Parent {\n    old: u32,\n}\n"
        self.commit_files({site: parent})
        issue, (finding,) = self.judged_findings(self.rule("read"), ((site, 1, "pub struct Parent {"),),
                                                 kind="rename", before="Parent", after="Renamed")
        self.commit_files({site: parent.replace("Parent", "Renamed")})
        code, output, errors, _ = self.refill_closing()
        self.assertEqual(code, 0, errors)
        reason = f"built in synced commit {self.tip}"
        self.assertIn(f"built {finding.finding_id} {finding.rule} {site}:1: struct Parent renamed to Renamed in {site}:1\n", output)
        self.assertIn(f"closed {issue}: {reason}\n", output)
        self.assertEqual((frontmatter(issue)["status"], frontmatter(issue)["reason"]), (["closed"], [reason]))
        self.assertEqual(self.ledger().proposal_findings(issue)[0].state, style_ledger.Built(issue))

    def test_decision_refill_waits_on_a_decision_whose_answer_changes_before_its_rename_is_built(self) -> None:
        site = self.sources[0]
        parent = "pub struct Parent {\n    old: u32,\n}\n"
        self.commit_files({site: parent})
        issue, (finding,) = self.judged_findings(self.rule("read"), ((site, 1, "pub struct Parent {"),),
                                                 kind="rename", before="Parent", after="Renamed")
        self.commit_files({site: parent.replace("Parent", "Renamed")})
        # The refill reads the decision as approved; the declaration step then reads another answer.
        with mock.patch.object(style_proposals, "approved_decision", return_value=False):
            output = self.refill_decisions()
        self.assertIn(f"waiting {issue}: no longer approved\n", output)
        self.assertEqual([(held.finding_id, held.state) for held in self.ledger().proposal_findings(issue)],
                         [(finding.finding_id, style_ledger.Filed(issue))])
        self.assertNotEqual(self.ledger().ticket_open("names-unit")[0]["kind"], "proposal")
        self.assertEqual(frontmatter(issue)["status"], ["open"])

    def test_decision_refill_after_a_merge_builds_only_the_unbuilt_findings(self) -> None:
        rule = self.rule("read")
        self.sync()
        issue = self.approved_decision(rule, self.sources[:2], "rename")
        self.write(self.sources[0], "pub enum Outcome { New }\n")
        self.write(self.sources[1], "pub enum Outcome { Old }\n")
        self.commit_to_every_checkout()
        ledger = self.ledger()
        applied, unapplied = (next(finding for finding in ledger.proposal_findings(issue) if finding.path == path)
                              for path in self.sources[:2])
        with mock.patch.object(style_proposals, "proposal_command"):
            self.assertEqual(self.run_next()[0], 0)
            first = ledger.ticket_open("names-unit")[0]
            self.assertEqual(sorted(first["finding_ids"]), sorted((applied.finding_id, unapplied.finding_id)))
            ledger.ticket_checkpoint(first["id"], self.tip)
            _ = ledger.ticket_merge(first["id"])
            code, _, errors = self.run_next()
        self.assertEqual(code, 0, errors)
        second = ledger.ticket_open("names-unit")[0]
        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual((second["kind"], second["files"], second["finding_ids"]),
                         ("proposal", [unapplied.path], [unapplied.finding_id]))

    def test_decision_refill_skips_a_merged_type_finding_until_a_person_marks_it_built(self) -> None:
        rule = self.rule("read")
        self.sync()
        issue = self.approved_decision(rule, (self.sources[0],), "type")
        ledger = self.ledger()
        finding, = ledger.proposal_findings(issue)
        with mock.patch.object(style_proposals, "proposal_command"):
            self.assertEqual(self.run_next()[0], 0)
        ticket = ledger.ticket_open("names-unit")[0]
        self.assertEqual((ticket["kind"], ticket["finding_ids"]), ("proposal", [finding.finding_id]))
        ledger.ticket_checkpoint(ticket["id"], self.tip)
        self.assertEqual(ledger.ticket_merge(ticket["id"]),
                         (style_ledger.FindingNotBuilt(finding.finding_id, approved_renames.NoAppliedCheck("type")),))
        held, = ledger.proposal_findings(issue)
        self.assertEqual((held.state, held.build_ticket), (style_ledger.Filed(issue), style_ledger.BuildTicket(ticket["id"])))
        code, _, errors, _ = self.refill_closing()
        self.assertEqual(code, 0, errors)
        self.assertEqual([item["id"] for item in ledger.proposal_tickets()], [ticket["id"]])
        self.assertEqual(frontmatter(issue)["status"], ["open"])
        ledger.mark_built(finding.finding_id)
        with mock.patch.object(style_proposals, "renumber"), redirect_stdout(io.StringIO()):
            style_proposals.complete(ledger)
        self.assertEqual((frontmatter(issue)["status"], frontmatter(issue)["reason"]), (["closed"], [f"built in {ticket['id']}"]))

    def test_decision_refill_closes_a_decision_whose_only_finding_is_in_a_removed_file(self) -> None:
        rule = self.rule("read")
        self.sync()
        issue = self.approved_decision(rule, (self.sources[1],), "rename")
        _ = self.git("rm", "-q", self.sources[1])
        self.commit_to_every_checkout()
        code, output, errors, renumbers = self.refill_closing()
        self.assertEqual(code, 0, errors)
        reason = f"nothing left to build: {self.sources[1]} not in synced commit {self.tip}"
        self.assertIn(f"closed {issue}: {reason}\n", output)
        self.assertEqual((frontmatter(issue)["status"], frontmatter(issue)["reason"]), (["closed"], [reason]))
        self.assertEqual(style_proposals.approved_decisions(issue.parent, "names-unit"), ())
        # No build merged, so complete does not renumber; the refill that closed the decision does, once.
        self.assertEqual(renumbers, 1)

    def test_decision_refill_closes_a_built_decision_whose_last_finding_is_in_a_removed_file(self) -> None:
        rule = self.rule("read")
        self.sync()
        issue = self.approved_decision(rule, self.sources[:2], "rename")
        ledger = self.ledger()
        built, gone = (next(finding for finding in ledger.proposal_findings(issue) if finding.path == path)
                       for path in self.sources[:2])
        self.write(built.path, "pub enum Outcome { New }\n")
        self.commit_to_every_checkout()
        ledger.ticket_add("names-unit-1", "names-unit", "proposal", "names", "crates/alpha", [built.path], [rule],
                          proposal=str(issue), finding_ids=(built.finding_id,))
        ledger.ticket_assign("names-unit-1", "main")
        ledger.ticket_checkpoint("names-unit-1", self.tip)
        _ = ledger.ticket_merge("names-unit-1")
        _ = self.git("rm", "-q", gone.path)
        self.commit_to_every_checkout()
        code, output, errors, renumbers = self.refill_closing()
        self.assertEqual(code, 0, errors)
        self.assertIn(f"closed {issue}: built in names-unit-1\n", output)
        self.assertEqual((frontmatter(issue)["status"], frontmatter(issue)["reason"]), (["closed"], ["built in names-unit-1"]))
        self.assertEqual(style_proposals.approved_decisions(issue.parent, "names-unit"), ())
        # Complete renumbers for the merged build, and the refill that closed the decision renumbers once.
        self.assertEqual(renumbers, 2)

    def test_decision_refill_skips_a_queued_ticket_made_before_decisions(self) -> None:
        rule = self.rule("read")
        self.sync()
        issue = self.approved_decision(rule, (self.sources[0],), "rename")
        ledger = self.ledger()
        finding = ledger.proposal_findings(issue)[0]
        ledger.ticket_add("names-unit-1", "names-unit", "proposal", "names", "crates/alpha", [finding.path], [rule],
                          proposal=str(issue), finding_ids=(finding.finding_id,))
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE tickets SET finding_ids='[]'")
            _ = connection.execute("UPDATE held_findings SET build_ticket=''")
        with mock.patch.object(style_proposals, "proposal_command"):
            code, output, errors = self.run_next()
        self.assertEqual(code, 0, errors)
        self.assertEqual(output.count("waiting names-unit-1: made before decisions; "
                                      + "drop it with style_ledger.py ticket drop, then run migrate\n"), 1)
        self.assertEqual([ticket["id"] for ticket in ledger.queued_tickets("names-unit")], ["names-unit-1"])
        opened = ledger.ticket_open("names-unit")[0]
        self.assertNotEqual(opened["id"], "names-unit-1")
        self.assertEqual(opened["finding_ids"], [finding.finding_id])

    def test_refill_stops_after_its_first_ticket_with_the_same_phase_and_output(self) -> None:
        rule = self.rule("first-ticket")
        paths = {name: f"crates/alpha/src/{name}.rs" for name in
                 ("first", "ready", "judged_changed", "unsynced", "structure", "busy", "gone")}
        self.commit_files({path: "pub fn work() {}\n" for name, path in paths.items() if name != "gone"})
        first = self.approved_decision(rule, (paths["first"],), "type")
        ledger = self.ledger()
        first_finding, = ledger.proposal_findings(first)
        decisions = [Issue(first, frontmatter(first))]
        later: list[style_ledger.HeldFinding] = []
        for name in ("ready", "judged_changed", "unsynced", "structure", "busy", "gone"):
            issue = first.with_name(name + ".md")
            _ = issue.write_text(first.read_text())
            finding = ledger.hold(rule, paths[name], 1, "change", "type", "before", "after",
                                  first_finding.rule_blob, "outside")
            ledger.file_held(finding.finding_id, issue)
            later.append(finding)
            decisions.append(Issue(issue, frontmatter(issue)))
        definitions = {definition.rule_id: definition for definition in rules.load_rules(self.repo)}
        busy = style_plan.BusyFiles(self.board(), frozenset({paths["busy"]}))
        original = self.plan.read_text()
        outcomes = tuple(style_ledger.DeclarationNotRead(finding) for finding in (first_finding, *later))
        follow = style_proposals.FollowRun(style_ledger.DeclarationReading(self.tip, outcomes), (), ())
        rank = style_proposals.RankedWork({paths["structure"]: frozenset({"types"})})
        baseline_output = io.StringIO()
        with (mock.patch.object(style_proposals, "approved_decisions", return_value=tuple(decisions[:1])),
              mock.patch.object(style_proposals, "run_follow", return_value=follow),
              mock.patch.object(style_proposals, "ranked_work", return_value=rank),
              mock.patch.object(style_proposals, "ranked_below_unit", return_value=False),
              mock.patch.object(style_proposals, "proposal_command"), redirect_stdout(baseline_output)):
            baseline = style_plan._select_phase(ledger, "names-unit", "demo-style-names", self.plan, self.repo,  # pyright: ignore[reportPrivateUsage]
                                               self.doc, original, unit_plans.AllCrates(), busy, frozenset(), definitions, style_plan.MainChanges(self.tip))
        self.assertIsInstance(baseline, style_plan.PhaseWritten)
        expected_phase = self.plan.read_bytes()
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE held_findings SET build_ticket=''")
            _ = connection.execute("DELETE FROM tickets WHERE unit='names-unit'")
        _ = self.plan.write_text(original)
        output = io.StringIO()

        with (mock.patch.object(style_proposals, "approved_decisions", return_value=tuple(decisions)),
              mock.patch.object(style_proposals, "run_follow", return_value=follow),
              mock.patch.object(style_proposals, "ranked_work", return_value=rank),
              mock.patch.object(style_proposals, "ranked_below_unit", return_value=False),
              mock.patch.object(style_proposals, "files_at", wraps=style_proposals.files_at) as read_paths,
              mock.patch.object(style_plan, "unsynced_changes", return_value={
                  paths["unsynced"]: style_plan.UnsyncedChange(paths["unsynced"], self.tip)}),
              mock.patch.object(style_proposals.RankExemptions, "allows", return_value=False),
              mock.patch.object(ledger, "decline_findings", wraps=ledger.decline_findings) as waive,
              mock.patch.object(style_proposals, "close_finished_decision", return_value=False) as close,
              mock.patch.object(ledger, "ticket_add", wraps=ledger.ticket_add) as add,
              mock.patch.object(ledger, "ticket_assign", wraps=ledger.ticket_assign) as assign,
              mock.patch.object(style_plan, "write_phase", wraps=style_plan.write_phase) as write,
              mock.patch.object(style_proposals, "proposal_command") as command,
              mock.patch.object(style_proposals, "renumber") as renumber, redirect_stdout(output)):
            result = style_plan._select_phase(ledger, "names-unit", "demo-style-names", self.plan, self.repo,  # pyright: ignore[reportPrivateUsage]
                                             self.doc, original, unit_plans.AllCrates(), busy, frozenset(), definitions, style_plan.MainChanges(self.tip))
        self.assertIsInstance(result, style_plan.PhaseWritten)
        read_paths.assert_called_once_with(self.repo, self.tip, (paths["first"],))
        self.assertEqual(self.plan.read_bytes(), expected_phase)
        self.assertEqual(baseline_output.getvalue(), f"waiting names: {paths['structure']} has open types work\n")
        self.assertEqual(output.getvalue(), baseline_output.getvalue())
        self.assertEqual(ledger.ticket_open("names-unit")[0]["finding_ids"], [first_finding.finding_id])
        self.assertEqual([(finding.state, finding.build_ticket) for issue in decisions[1:]
                          for finding in ledger.proposal_findings(issue.path)],
                         [(style_ledger.Filed(issue.path), style_ledger.NoBuildTicket()) for issue in decisions[1:]])
        waive.assert_not_called()
        close.assert_not_called()
        self.assertEqual(add.call_count, 1)
        self.assertEqual(assign.call_count, 1)
        self.assertEqual(write.call_count, 1)
        command.assert_called_once_with("start", str(first))
        renumber.assert_not_called()

if __name__ == "__main__":
    _ = unittest.main()
