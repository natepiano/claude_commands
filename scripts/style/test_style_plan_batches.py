"""Large decision batches retain every finding and release dropped slices."""

from __future__ import annotations

import sys
import subprocess
import unittest
from dataclasses import replace
from pathlib import Path
from typing import final
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import approved_renames
import style_plan
import style_proposals

import berth_board
import rules
import style_ledger
from issues import Issue, frontmatter

from style_plan_test_support import StylePlanFixture


@final
class StylePlanTests(StylePlanFixture):
    def test_secondary_structure_findings_cannot_lower_the_line_cap_below_the_primary_batch(self) -> None:
        primary = self.rule("a-renames")
        secondary = self.rule("b-structure")
        files = self.sources[:2]
        self.commit_files({path: "fn work() {}\n" * 2000 for path in files})
        first = self.approved_decision(primary, files, "rename", name="a-first")
        second = self.approved_decision(secondary, files, "restructure", name="b-second")
        candidates = self.buildable_decisions((first, second))
        batch = style_plan.fill_decision_batch(self.repo, self.tip, candidates, "names")
        self.assertEqual(batch.files, files)
        self.assertEqual(batch.decisions, (first,))
        self.assertEqual({finding.rule for finding in batch.findings}, {primary})
        with self.ledger() as ledger:
            self.assertTrue(all(isinstance(finding.build_ticket, style_ledger.NoBuildTicket)
                                for finding in ledger.proposal_findings(second)))
        later = style_plan.fill_decision_batch(self.repo, self.tip, candidates[1:], "names")
        self.assertEqual(later.files, files[:1])
        self.assertEqual(later.decisions, (second,))

    def test_secondary_findings_count_shared_files_and_lines_once(self) -> None:
        primary = self.rule("a-renames")
        secondary = self.rule("b-structure")
        files = self.sources[:2]
        self.commit_files({path: "fn work() {}\n" * 1500 for path in files})
        first = self.approved_decision(primary, files, "rename", name="a-first")
        second = self.approved_decision(secondary, files, "restructure", name="b-second")
        batch = style_plan.fill_decision_batch(self.repo, self.tip, self.buildable_decisions((first, second)), "names")
        self.assertEqual(batch.files, files)
        self.assertEqual(batch.decisions, (first, second))
        self.assertEqual(len(batch.findings), 4)

    def test_unselected_structure_finding_does_not_limit_selected_rename_files(self) -> None:
        rule = self.rule("mixed")
        secondary_rule = self.rule("other-structure")
        files = tuple(f"crates/alpha/src/piece_{index}.rs" for index in range(7))
        last = "crates/alpha/src/z.rs"
        self.commit_files({**{path: "fn work() {}\n" for path in files}, last: "fn work() {}\n" * 6000})
        first = self.approved_decision(rule, files, "rename", name="a-first")
        second = self.approved_decision(rule, (last,), "restructure", name="b-second")
        third = self.approved_decision(secondary_rule, files, "restructure", name="c-third")
        batch = style_plan.fill_decision_batch(self.repo, self.tip, self.buildable_decisions((first, second, third)), "names")
        self.assertEqual(batch.files, files)
        self.assertEqual(batch.decisions, (first,))

    def test_oversized_first_file_forms_a_batch_alone(self) -> None:
        rule = self.rule("structure")
        files = self.sources[:2]
        self.commit_files({files[0]: "fn work() {}\n" * 3001, files[1]: "fn work() {}\n"})
        issue = self.approved_decision(rule, files, "restructure")
        batch = style_plan.fill_decision_batch(self.repo, self.tip, self.buildable_decisions((issue,)), "names")
        self.assertEqual(batch.files, files[:1])
        self.assertEqual(tuple(finding.path for finding in batch.findings), files[:1])
        self.assertEqual(batch.decisions, (issue,))

    def test_capped_ticket_excludes_a_later_decision_without_selected_findings(self) -> None:
        rule = self.rule("types")
        files = tuple(f"crates/alpha/src/piece_{index:02}.rs" for index in range(26))
        self.commit_files({path: "fn work() {}\n" for path in files})
        first = self.approved_decision(rule, files[:25], "type", name="a-first")
        second = self.approved_decision(rule, files[25:], "type", name="b-second")
        _ = self.refill_decisions()
        with self.ledger() as ledger:
            ticket, = ledger.ticket_open("names-unit")
            self.assertEqual(ticket["files"], list(files[:25]))
            self.assertEqual(ticket["proposal"], str(first))
            self.assertEqual(ledger.ticket_proposals(ticket["id"]), (first,))
            self.assertTrue(all(isinstance(finding.build_ticket, style_ledger.NoBuildTicket)
                                for finding in ledger.proposal_findings(second)))

    def test_capped_ticket_chooses_a_primary_that_contributes_selected_findings(self) -> None:
        rule = self.rule("types")
        files = tuple(f"crates/alpha/src/piece_{index:02}.rs" for index in range(26))
        self.commit_files({path: "fn work() {}\n" for path in files})
        first = self.approved_decision(rule, files[25:], "type", name="a-first")
        second = self.approved_decision(rule, files[:25], "type", name="b-second")
        _ = self.refill_decisions()
        with self.ledger() as ledger:
            ticket, = ledger.ticket_open("names-unit")
            self.assertEqual(ticket["files"], list(files[:25]))
            self.assertEqual(ticket["proposal"], str(second))
            self.assertEqual(ledger.ticket_proposals(ticket["id"]), (second,))
            self.assertTrue(all(isinstance(finding.build_ticket, style_ledger.NoBuildTicket)
                                for finding in ledger.proposal_findings(first)))

    def test_each_verification_invocation_runs_each_stage_even_when_inputs_change(self) -> None:
        ticket_id = self.verification_ticket()
        runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0))
        with mock.patch.object(style_plan, "run_verification_step", runner):
            for profile in ("default", "default", "changed-policy"):
                with mock.patch.dict("os.environ", {"NEXTEST_PROFILE": profile, "LINT_CONFIG_FILE": profile}):
                    self.assertEqual(style_plan.verify_ticket(self.unit, ticket_id), 0)
            self.assertEqual(runner.call_args_list, [mock.call(self.unit, stage, "alpha")
                             for _ in range(3) for stage in ("check", "lint", "test")])

    def test_combined_ticket_closes_each_decision_with_the_same_build_credit(self) -> None:
        issues, ticket_id = self.combined_decision_ticket()
        with self.ledger() as ledger:
            ticket, = ledger.ticket_open("names-unit")
            ledger.ticket_checkpoint(ticket_id, self.tip)
            _ = ledger.ticket_merge(ticket_id)
            for identity in ticket["finding_ids"]:
                ledger.mark_built(identity)
            with mock.patch.object(style_proposals, "proposal_command") as command:
                for issue in issues:
                    with self.subTest(decision=issue.name):
                        self.assertTrue(style_proposals.close_finished_decision(ledger, issue))
                        command.assert_any_call("close", str(issue), "--reason", f"built in {ticket_id}")

    def test_combined_ticket_holds_each_decision_during_migration(self) -> None:
        issues, ticket_id = self.combined_decision_ticket()
        with self.ledger() as ledger:
            plan = style_proposals.plan_moves(ledger, issues[0].parent,
                                             [Issue(issue, frontmatter(issue)) for issue in issues],
                                             style_proposals.ResumeMigration())
            self.assertEqual(plan.refusals, tuple(f"{issue}: live build ticket {ticket_id} (assigned)" for issue in issues))
            self.assertEqual(plan.sources, ())

    def test_combined_ticket_guards_each_decision_from_reopening(self) -> None:
        issues, ticket_id = self.combined_decision_ticket()
        with self.ledger() as ledger:
            for issue in issues:
                with self.subTest(decision=issue.name), self.assertRaisesRegex(
                        style_ledger.LedgerError, f"open ticket {ticket_id} builds it"):
                    _ = style_proposals.file_unapplied_again(ledger, issue, "parent", style_proposals.CHECK_ONLY,
                                                            dry_run=True)

    def test_mechanical_decision_fills_the_read_cap_instead_of_balancing_structure_slices(self) -> None:
        rule = self.rule("prefer-type-named-fields-and-bindings")
        files = tuple(f"crates/alpha/src/piece_{index:02}.rs" for index in range(30))
        self.commit_files({path: "fn work() { let old = 1; consume(old); }\n" for path in files})
        issue = self.approved_decision(rule, files, "rename")
        _ = self.refill_decisions()
        with self.ledger() as ledger:
            ticket, = ledger.ticket_open("names-unit")
            self.assertEqual(ticket["files"], list(files[:25]))
            self.assertEqual(len(ticket["finding_ids"]), 25)
            self.assertEqual(sum(isinstance(f.build_ticket, style_ledger.NoBuildTicket)
                                 for f in ledger.proposal_findings(issue)), 5)

    def test_refill_combines_approved_rules_on_selected_files_and_rechecks_each_approval(self) -> None:
        primary = self.rule("a-first")
        secondary = self.rule("b-second")
        self.sync()
        first = self.approved_decision(primary, self.sources[:2], "type", name="a-first")
        second = self.approved_decision(secondary, (self.sources[0], self.sources[2]), "type", name="b-second")
        _ = self.refill_decisions()
        with self.ledger() as ledger:
            ticket, = ledger.ticket_open("names-unit")
            self.assertEqual(ticket["files"], list(self.sources[:2]))
            self.assertEqual(ticket["rules"], [primary, secondary])
            self.assertEqual(set(ticket["finding_ids"]), {f.finding_id for issue in (first, second)
                              for f in ledger.proposal_findings(issue) if f.path in self.sources[:2]})
            origin = ledger.ticket_origin(ticket["id"])
            assert isinstance(origin, style_ledger.ApprovedProposal)
            self.assertEqual(len(style_plan.approved_ticket(ledger, ticket["id"], origin).findings), 3)
            _ = second.write_text(second.read_text().replace("plan: approved", "style_answer: declined\nstyle_answer_words: rejected"))
            with self.assertRaisesRegex(style_ledger.LedgerError, "approval"):
                _ = style_plan.approved_ticket(ledger, ticket["id"], origin)

    def test_mechanical_restructure_uses_the_read_cap_and_respects_busy_and_declined_sites(self) -> None:
        rule = self.rule("module-structure")
        files = tuple(f"crates/alpha/src/piece_{index:02}.rs" for index in range(10))
        self.commit_files({path: "fn work() {}\n" for path in files})
        issue = self.approved_decision(rule, files, "restructure")
        with self.ledger() as ledger:
            declined = next(f for f in ledger.proposal_findings(issue) if f.path == files[0])
            _ = ledger.decline_findings((declined.finding_id,), "declined", style_ledger.WaiveByLedger())
        with mock.patch.object(berth_board, "read_board", return_value=self.board(files[1])):
            _ = self.refill_decisions()
        with self.ledger() as ledger:
            ticket, = ledger.ticket_open("names-unit")
            self.assertEqual(ticket["files"], list(files[2:]))
            self.assertEqual(len(ticket["finding_ids"]), 8)
            self.assertNotIn(declined.finding_id, ticket["finding_ids"])

    def test_rename_only_ticket_has_one_writer_and_no_tester(self) -> None:
        rule = self.rule("prefer-type-named-fields-and-bindings")
        self.sync()
        _ = self.approved_decision(rule, self.sources[:2], "rename")
        _ = self.refill_decisions()
        workers = self.plan.read_text().split("**Workers:**\n", 1)[1].split("\n\n", 1)[0]
        self.assertEqual(workers, "1 writer; no tester.")

    def test_cfg_test_only_ticket_has_one_writer_but_production_or_escaping_edits_need_a_tester(self) -> None:
        path = self.sources[0]
        self.commit_files({path: "pub fn production() {} // production comment\n#[cfg(test)]\nmod tests {\n    fn check() { consume(42); }\n}\n"})
        rule = self.rule("values")
        issue = self.approved_decision(rule, (), "restructure")
        with self.ledger() as ledger:
            blob = next(r.blob for r in rules.load_rules(self.repo) if r.rule_id == rule)
            finding = ledger.hold(rule, path, 4, "    fn check() { consume(42); }", "restructure", "42", "43", blob, "outside")
            ledger.file_held(finding.finding_id, issue)
        _ = self.refill_decisions()
        self.assertIn("**Workers:**\n1 writer; no tester.\n", self.plan.read_text())
        with self.ledger() as ledger:
            ticket, = ledger.ticket_open("names-unit")
            for changed in (replace(finding, line=1, before="production", after="changed"),
                            replace(finding, line=1, before="production comment", after="changed comment"),
                            replace(finding, after="42); } } pub fn escaped() {} #[cfg(test)] mod more { fn check() { consume(43")):
                with self.subTest(change=changed.after):
                    self.assertEqual(style_plan.ticket_review_scope(self.repo, ticket, (changed,)),
                                     style_plan.ReviewScope.PRODUCTION)

    def test_verification_refuses_changes_in_another_crate(self) -> None:
        ticket_id = self.verification_ticket()
        _ = (self.unit / self.sources[2]).write_text("pub fn another_crate() {}\n")
        with mock.patch.object(style_plan, "run_verification_step") as runner:
            with self.assertRaisesRegex(style_ledger.LedgerError, "crosses crate boundaries"):
                _ = style_plan.verify_ticket(self.unit, ticket_id)
            runner.assert_not_called()

    def test_verification_stops_on_a_failed_stage_or_a_changed_tree(self) -> None:
        ticket_id = self.verification_ticket()
        with mock.patch.object(style_plan, "run_verification_step",
                               side_effect=[subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 1)]) as runner:
            self.assertEqual(style_plan.verify_ticket(self.unit, ticket_id), 1)
            self.assertEqual(runner.call_args_list, [mock.call(self.unit, stage, "alpha") for stage in ("check", "lint")])

        def edit_during_check(_repo: Path, _stage: str, _package: str) -> subprocess.CompletedProcess[bytes]:
            _ = (self.unit / self.sources[0]).write_text("pub fn changed_during_check() {}\n")
            return subprocess.CompletedProcess([], 0)

        with mock.patch.object(style_plan, "run_verification_step", side_effect=edit_during_check) as runner:
            with self.assertRaisesRegex(style_ledger.LedgerError, "changed during check"):
                _ = style_plan.verify_ticket(self.unit, ticket_id)
            runner.assert_called_once_with(self.unit, "check", "alpha")

    def test_decision_refill_slices_forty_restructure_files_and_releases_dropped_slice(self) -> None:
        rule = self.rule("decision", "tier: C\nfamily: names\ninput_scope: file\nmode: propose\n")
        files = tuple(f"crates/alpha/src/piece_{index:02}.rs" for index in range(40))
        for path in files:
            self.write(path, "fn work() {}\n")
        self.tip = self.commit()
        _ = self.git("merge", "--ff-only", "main", cwd=self.unit)
        _ = self.git("merge", "--ff-only", "main", cwd=self.style)
        self.sync()
        folder = self.root / "issues"
        folder.mkdir()
        issue = folder / "decision.md"
        ledger = self.ledger()
        blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == rule)
        for path in files:
            finding = ledger.hold(rule, path, 1, "split", "restructure", "before", "after", blob, "outside")
            ledger.file_held(finding.finding_id, issue)
        _ = issue.write_text("---\nstatus: open\ntags: [proposal, style]\nunit: names-unit\nplan: approved\n"
                             + f"style_rule: {rule}\nstyle_crate: crates/alpha\nstyle_rule_blob: {blob}\n---\n")
        _ = self.doc.write_text(self.doc.read_text().replace("## Units", f"- **Proposals:** {folder}\n\n## Units"))
        first_slice = style_plan.read_slices(self.repo, self.tip, files, style_plan.BatchLimits(6, 3000, 5))[0]
        with (mock.patch.object(berth_board, "read_board", return_value=self.board(*first_slice)),
              mock.patch.object(style_proposals, "proposal_command")):
            code, _, errors = self.run_next()
        self.assertEqual(code, 0, errors)
        first_ticket = ledger.ticket_open("names-unit")[0]
        self.assertLessEqual(len(first_ticket["files"]), 6)
        self.assertFalse(set(first_ticket["files"]) & set(first_slice))
        ledger.ticket_drop(first_ticket["id"])
        self.assertTrue(all(isinstance(finding.build_ticket, style_ledger.NoBuildTicket)
                            for finding in ledger.proposal_findings(issue)))
        built: set[str] = set()
        with mock.patch.object(style_proposals, "proposal_command"):
            for _ in range(40):
                code, _, errors = self.run_next()
                self.assertEqual(code, 0, errors)
                ticket = ledger.ticket_open("names-unit")[0]
                self.assertEqual(ticket["kind"], "proposal")
                self.assertLessEqual(len(ticket["files"]), 6)
                self.assertFalse(built & set(ticket["files"]))
                built.update(ticket["files"])
                ledger.ticket_checkpoint(ticket["id"], self.tip)
                not_built = ledger.ticket_merge(ticket["id"])
                self.assertEqual(not_built, tuple(style_ledger.FindingNotBuilt(identity, approved_renames.NoAppliedCheck("restructure"))
                                                  for identity in sorted(ticket["finding_ids"])))
                for identity in ticket["finding_ids"]:
                    ledger.mark_built(identity)
                if len(built) == 40:
                    break
        self.assertEqual(built, set(files))
        self.assertTrue(all(isinstance(finding.state, style_ledger.Built) for finding in ledger.proposal_findings(issue)))

if __name__ == "__main__":
    _ = unittest.main()
