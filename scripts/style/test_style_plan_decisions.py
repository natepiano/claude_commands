"""Approved decision tickets, rank exemptions and site proof."""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from pathlib import Path
from typing import final
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import style_plan
import style_proposals

import rules
import style_ledger

from style_plan_test_support import StylePlanFixture


@final
class StylePlanTests(StylePlanFixture):
    def test_fix_worker_applies_after_as_the_whole_before_span_replacement(self) -> None:
        ticket_id = self.approved_reference_ticket()
        with self.ledger() as ledger:
            ticket, = ledger.queued_tickets("names-unit")
            text = style_plan.phase_text(ticket, ledger.pinned_rule_texts(ticket_id), self.plan, self.plan_header())
        authorization = text.split("Read the issue for its sites and before/after changes.", 1)[1]
        authorization = authorization.split("Hold every other non-mechanical finding.", 1)[0]
        self.assertIn("The After text replaces the whole Before span.", authorization)
        self.assertIn("never the old wording with a new line added", authorization)
        self.assertIn("Rewrite a comment to name the constructs and keep every reason.", authorization)

    def test_completed_classified_catalog_waits_for_sync_without_creating_work(self) -> None:
        rule = self.rule("read")
        self.sync()
        for path in self.sources:
            self.record(rule, path, self.tip)
        before = self.plan.read_bytes()
        self.assertEqual(self.run_next(), (3, "waiting for sync\n", ""))
        self.assertEqual(self.plan.read_bytes(), before)
        self.assertEqual(self.count_tickets(), 0)

    def test_member_rule_and_global_same_stem_have_separate_ids_and_reach(self) -> None:
        global_rule = self.rule("same")
        path = "docs/alpha/style/same.md"
        self.write(path, "---\ntier: C\nfamily: names\ninput_scope: file\n---\nLocal instructions.\n")
        self.tip = self.commit()
        self.sync()
        tickets = self.candidates()
        member = "local:" + path
        alpha = [ticket for ticket in tickets if ticket.crate == "crates/alpha"]
        beta = [ticket for ticket in tickets if ticket.crate == "crates/beta"]
        self.assertEqual({rule for ticket in alpha for rule in ticket.rules}, {global_rule, member})
        self.assertEqual({rule for ticket in beta for rule in ticket.rules}, {global_rule})

    def test_prefilter_word_and_regex_shell_command_create_non_rust_listed_tickets(self) -> None:
        self.write("scripts/test.sh", "#!/bin/sh\ncargo test -p sample # forbidden_marker\n")
        self.tip = self.commit()
        word = self.rule("words", "tier: B\nfamily: names\npre_filter: forbidden_marker\n")
        shell = self.rule("shell", "tier: B\nfamily: names\ncandidates:\n  kind: regex\n  pattern: cargo test\n  globs: [scripts/*.sh]\n  surface: whole\n")
        self.sync()
        tickets = self.candidates()
        self.assertEqual({(ticket.rule, ticket.files) for ticket in tickets}, {(word, ("scripts/test.sh",)), (shell, ("scripts/test.sh",))})

    def test_refill_uses_the_units_plan_and_records_branch_and_pinned_rule_paths(self) -> None:
        rule = self.rule("read")
        self.sync()
        _ = self.plan.write_text(self.plan.read_text() + "\n## Notes\nKeep this section.\n")
        showrunner = self.style / "docs/style-production/units/names-unit.md"
        before = showrunner.read_bytes()
        self.assertEqual(self.run_next(), (0, "phase written: names-unit-1\n", ""))
        ticket = self.ledger().ticket_open("names-unit")[0]
        self.assertEqual(ticket["branch"], "refs/heads/demo-style-names")
        self.assertEqual(ticket["rules"], [rule])
        pinned = self.ledger().pinned_rule_texts(ticket["id"])[0]
        rule_file = self.plan.parent.parent / "rules" / (pinned.blob + ".md")
        self.assertEqual(rule_file.read_bytes(), (self.rule_dir / "read.md").read_bytes())
        self.assertIn(f"`{rule_file}`", self.plan.read_text())
        self.assertIn(f"blob `{ticket['rule_blobs'][rule]}`", self.plan.read_text())
        self.assertLess(self.plan.read_text().index("### Phase 1"), self.plan.read_text().index("## Notes"))
        self.assertTrue(self.plan.read_text().endswith("## Notes\nKeep this section.\n"))
        self.assertEqual(showrunner.read_bytes(), before)
        self.assertFalse((showrunner.parent.parent / "rules").exists())
        content = self.plan.read_bytes()
        self.assertEqual(self.run_next(), (0, "resume names-unit-1\n", ""))
        self.assertEqual(self.plan.read_bytes(), content)
        self.assertEqual(self.count_tickets(), 1)

    def test_each_interruption_rebuilds_the_uninterrupted_phase_byte_for_byte(self) -> None:
        _ = self.rule("read")
        self.sync()
        database = self.ledger().database
        snapshot = database.read_bytes()
        header = self.plan.read_bytes()
        self.assertEqual(self.run_next()[0], 0)
        uninterrupted = self.plan.read_bytes()
        pinned_dir = self.plan.parent.parent / "rules"
        for boundary in ("added", "assigned", "pinned"):
            with self.subTest(boundary=boundary):
                _ = database.write_bytes(snapshot)
                _ = self.plan.write_bytes(header)
                shutil.rmtree(pinned_dir)
                if boundary == "added":
                    with mock.patch.object(style_ledger.Ledger, "ticket_assign", side_effect=RuntimeError("interruption")):
                        with self.assertRaisesRegex(RuntimeError, "interruption"):
                            _ = style_plan.next_phase("names-unit", self.plan, self.repo)
                elif boundary == "assigned":
                    with mock.patch.object(style_plan, "write_phase", side_effect=RuntimeError("interruption")):
                        with self.assertRaisesRegex(RuntimeError, "interruption"):
                            _ = style_plan.next_phase("names-unit", self.plan, self.repo)
                else:
                    real_write = style_plan.atomic_write
                    def fail_plan(path: Path, data: bytes) -> None:
                        if path == self.plan:
                            raise RuntimeError("interruption")
                        return real_write(path, data)
                    with mock.patch.object(style_plan, "atomic_write", fail_plan):
                        with self.assertRaisesRegex(RuntimeError, "interruption"):
                            _ = style_plan.next_phase("names-unit", self.plan, self.repo)
                    self.assertTrue(pinned_dir.exists())
                self.assertEqual(self.run_next()[0], 0)
                self.assertEqual(self.plan.read_bytes(), uninterrupted)
                self.assertEqual(self.count_tickets(), 1)
                self.assertEqual(self.ledger().ticket_open("names-unit")[0]["branch"], "refs/heads/demo-style-names")

    def test_resume_restores_a_truncated_pin_without_changing_the_phase(self) -> None:
        _ = self.rule("read")
        self.sync()
        self.assertEqual(self.run_next()[0], 0)
        expected = self.plan.read_bytes()
        pinned = self.ledger().pinned_rule_texts("names-unit-1")[0]
        path = self.plan.parent.parent / "rules" / (pinned.blob + ".md")
        _ = path.write_bytes(pinned.text[:len(pinned.text) // 2])
        self.assertEqual(self.run_next(), (0, "resume names-unit-1\n", ""))
        self.assertEqual(path.read_bytes(), pinned.text)
        self.assertEqual(self.git("hash-object", str(path)), pinned.blob)
        self.assertEqual(self.plan.read_bytes(), expected)
        self.assertEqual(self.count_tickets(), 1)

    def test_resume_restores_a_truncated_phase_byte_for_byte(self) -> None:
        _ = self.rule("read")
        self.sync()
        self.assertEqual(self.run_next()[0], 0)
        expected = self.plan.read_bytes()
        for cut in (len(expected) // 2, expected.index(b"status: todo") + len(b"status: todo")):
            with self.subTest(cut=cut):
                _ = self.plan.write_bytes(expected[:cut])
                self.assertEqual(self.run_next(), (0, "resume names-unit-1\n", ""))
                self.assertEqual(self.plan.read_bytes(), expected)
        self.assertEqual(self.count_tickets(), 1)

    def test_interrupted_replacement_preserves_final_pin_and_plan_files(self) -> None:
        _ = self.rule("read")
        self.sync()
        self.assertEqual(self.run_next()[0], 0)
        expected = self.plan.read_bytes()
        pinned = self.ledger().pinned_rule_texts("names-unit-1")[0]
        pin = self.plan.parent.parent / "rules" / (pinned.blob + ".md")
        for target in (pin, self.plan):
            with self.subTest(target=target):
                before = target.read_bytes()
                with mock.patch.object(os, "replace", side_effect=OSError("interrupted replacement")):
                    with self.assertRaisesRegex(OSError, "interrupted replacement"):
                        style_plan.atomic_write(target, b"incomplete")
                self.assertEqual(target.read_bytes(), before)
                self.assertEqual(tuple(target.parent.glob(f".{target.name}.*")), ())
        self.assertEqual(self.plan.read_bytes(), expected)

    def test_decision_ticket_text_separates_rule_findings_from_candidate_ideas(self) -> None:
        _ = self.rule("read")
        self.sync()
        self.assertEqual(self.run_next()[0], 0)
        text = self.plan.read_text()
        self.assertIn("Hold a finding only when the cited rule's text requires the change.", text)
        self.assertIn('Record other ideas with `style_ledger.py candidate --ticket names-unit-1 --site <path>:<line> --idea "<one line>"`.', text)

    def test_decision_gate_refuses_an_answer_declined_after_ticket_creation(self) -> None:
        ticket = self.approved_reference_ticket()
        origin = self.ledger().ticket_origin(ticket)
        assert isinstance(origin, style_ledger.ApprovedProposal)
        _ = origin.issue.write_text(origin.issue.read_text().replace("status: open", "status: open\nstyle_answer: declined\nstyle_answer_words: No"))
        code, message = self.reference_gate(ticket)
        self.assertEqual(code, 1)
        self.assertIn("approval", message)

    def test_decision_gate_uses_ticket_findings_not_other_findings_of_the_issue(self) -> None:
        ticket = self.approved_reference_ticket()
        ledger = self.ledger()
        origin = ledger.ticket_origin(ticket)
        assert isinstance(origin, style_ledger.ApprovedProposal)
        first = ledger.proposal_findings(origin.issue)[0]
        other = ledger.hold(first.rule, self.sources[1], 1, "another change", "type", "u32", "u64", first.rule_blob, "outside")
        ledger.file_held(other.finding_id, origin.issue)
        self.write(self.sources[1], "pub fn changed_without_this_ticket() {}\n")
        self.assertEqual(self.reference_gate(ticket)[0], 1)
        self.write(self.sources[1], "pub fn value() {}\n")
        self.write(self.sources[0], "pub fn approved_change() {}\nenum Outcome { NewName }\n")
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def test_decision_gate_refuses_until_each_approved_rename_is_applied(self) -> None:
        ticket = self.approved_reference_ticket()
        origin = self.ledger().ticket_origin(ticket)
        assert isinstance(origin, style_ledger.ApprovedProposal)
        finding_id, = origin.finding_ids
        self.write(self.sources[0], "enum Outcome { OldName }\n")
        self.assertEqual(self.reference_gate(ticket), (1, f"review: {finding_id}: Outcome::OldName -> Outcome::NewName "
                                                          + f"not applied in {self.sources[0]}\n"))
        self.write(self.sources[0], "enum Outcome { NewName }\n")
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def test_decision_gate_skips_a_yielded_finding_and_refill_takes_it_again(self) -> None:
        ticket = self.approved_reference_ticket()
        self.sync()
        ledger = self.ledger()
        origin = ledger.ticket_origin(ticket)
        assert isinstance(origin, style_ledger.ApprovedProposal)
        kept, = origin.finding_ids
        rule = ledger.proposal_findings(origin.issue)[0].rule
        freed = ledger.hold(rule, self.sources[3], 1, "rename", "rename", "Shape::Round", "Shape::Square",
                            ledger.proposal_findings(origin.issue)[0].rule_blob, "outside")
        ledger.file_held(freed.finding_id, origin.issue)
        ledger.ticket_drop(ticket)
        ledger.ticket_add("names-unit-2", "names-unit", "proposal", "names", "crates/alpha", [self.sources[0], self.sources[3]],
                          [rule], proposal=str(origin.issue), finding_ids=(kept, freed.finding_id))
        ledger.ticket_assign("names-unit-2", "main")
        self.write(self.sources[3], "enum Shape { Round }\n")
        self.assertEqual(self.reference_gate("names-unit-2"), (1, f"review: {freed.finding_id}: Shape::Round -> Shape::Square "
                                                                  + f"not applied in {self.sources[3]}\n"))
        self.assertEqual(style_plan._unbuilt_findings(ledger, origin.issue), ())  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(ledger.ticket_yield("names-unit-2", self.sources[3]), (freed.finding_id,))
        _ = subprocess.run(["git", "-C", str(self.repo), "checkout", "--", self.sources[3]], check=True)
        self.assertEqual(self.reference_gate("names-unit-2"), (0, "approved proposal\n"))
        self.assertEqual([finding.finding_id for finding in style_plan._unbuilt_findings(ledger, origin.issue)],  # pyright: ignore[reportPrivateUsage]
                         [freed.finding_id])

    def test_no_magic_decision_tickets_take_every_file_and_all_its_findings(self) -> None:
        rule = self.rule("no-magic-values")
        self.sync()
        issue = self.approved_decision(rule, self.sources[:2], "restructure")
        ledger = self.ledger()
        blob = next(rule_.blob for rule_ in rules.load_rules(self.repo) if rule_.rule_id == rule)
        second = ledger.hold(rule, self.sources[0], 1, "another value", "restructure", "42", "LIMIT", blob, "outside")
        ledger.file_held(second.finding_id, issue)
        _ = self.refill_decisions()
        ticket, = ledger.ticket_open("names-unit")
        self.assertEqual(ticket["files"], list(self.sources[:2]))
        self.assertEqual(set(ticket["finding_ids"]), {f.finding_id for f in ledger.proposal_findings(issue)})

    def test_queued_and_new_decisions_share_unchanged_site_proof_and_followed_coordinates(self) -> None:
        _ = self.types_unit()
        rule = self.rule("read")
        site = self.sources[0]
        source = "fn work() {\n    consume(791);\n}\n"
        self.commit_files({site: source})
        issue, (finding,) = self.judged_findings(rule, ((site, 2, source.splitlines()[1]),), kind="restructure")
        self.write_generated_body(issue)
        ledger = self.ledger()
        self.record(rule, site, self.tip, "modules-unit-1")
        self.commit_files({site: "fn sibling() {}\n" + source})
        _ = self.refill_decisions()
        ticket, = ledger.ticket_open("names-unit")
        self.assertEqual((ticket["kind"], ticket["finding_ids"]), ("proposal", [finding.finding_id]))
        self.assertEqual(ledger.named_held([finding.finding_id])[0].line, 3)
        ledger.ticket_drop(ticket["id"])
        ledger.ticket_add("names-unit-99", "names-unit", "proposal", "names", "crates/alpha", [site], [rule],
                          proposal=str(issue), finding_ids=(finding.finding_id,))
        self.commit_files({site: "fn another() {}\nfn sibling() {}\n" + source})
        _ = self.refill_decisions()
        ticket, = ledger.ticket_open("names-unit")
        self.assertEqual(ticket["id"], "names-unit-99")
        self.assertEqual(ledger.named_held([finding.finding_id])[0].line, 4)

    def test_site_follow_does_not_clear_clean_cell_after_a_new_literal_is_added(self) -> None:
        rule = self.rule("no-magic-values", "tier: C\nfamily: values\ninput_scope: file\n")
        names = self.rule("read")
        path = self.sources[0]
        source = "fn work() {\n    consume(LIMIT);\n}\n"
        self.commit_files({path: source})
        _, (finding,) = self.judged_findings(names, ((path, 2, source.splitlines()[1]),), kind="restructure")
        ledger = self.ledger()
        self.record(rule, path, self.tip, "outside")
        self.commit_files({path: "fn newly_bad() { consume(8675309); }\n" + source})
        _ = ledger.follow_declarations([finding.finding_id], ledger.synced_commit(), lambda _: True)
        self.assertEqual(ledger.named_held([finding.finding_id])[0].line, 3)
        self.assertEqual(next(cell.state for cell in ledger.cells(rule=rule) if cell.path == path), "stale")

    def test_decision_b_refill_and_queued_ticket_keep_only_rank_exempt_bindings(self) -> None:
        _ = self.types_unit()
        _ = self.rule("types-read", "tier: C\nfamily: types\ninput_scope: file\n")
        rule = self.rule("prefer-type-named-fields-and-bindings")
        source = "pub struct Parent { old: u32 }\nfn f() {\n    let old = 1;\n    consume(old);\n}\n"
        self.commit_files({self.sources[0]: source})
        issue, findings = self.judged_findings(rule, ((self.sources[0], 1, source.splitlines()[0]),
                                                     (self.sources[0], 3, source.splitlines()[2])),
                                              kind="rename", before="old", after="fresh")
        _ = self.refill_decisions()
        ledger = self.ledger()
        ticket, = ledger.ticket_open("names-unit")
        self.assertEqual((ticket["kind"], ticket["finding_ids"]), ("proposal", [findings[1].finding_id]))
        ledger.ticket_drop(ticket["id"])
        ledger.ticket_add("names-unit-99", "names-unit", "proposal", "names", "crates/alpha", [self.sources[0]], [rule],
                          proposal=str(issue), finding_ids=(findings[1].finding_id,))
        _ = self.refill_decisions()
        self.assertEqual(ledger.ticket_open("names-unit")[0]["id"], "names-unit-99")
        # The exemption never lifts the separate changed-since-judgment gate.
        ledger.ticket_drop("names-unit-99")
        self.commit_files({self.sources[0]: source + "fn unrelated() {}\n"})
        with mock.patch.object(style_proposals, "proposal_command"):
            _, output, _ = self.run_next()
        self.assertIn(f"waiting {issue}: {self.sources[0]} changed since it was judged; source follow could not refresh the decision (2 findings)", output)
        self.assertFalse(any(t["kind"] == "proposal" for t in ledger.ticket_open("names-unit")))

    def test_decision_refill_skips_a_rule_that_left_the_catalog_and_coverage_goes_on(self) -> None:
        gone = self.rule("gone-decision")
        _ = self.rule("read")
        self.sync()
        issue = self.approved_decision(gone, (self.sources[0],), "rename")
        (self.rule_dir / "gone-decision.md").unlink()
        with mock.patch.object(style_proposals, "proposal_command") as start:
            code, output, errors = self.run_next()
        self.assertEqual(code, 0, errors)
        self.assertEqual(output.count(f"waiting {issue}: rule {gone} not in the catalog\n"), 1)
        start.assert_not_called()
        self.assertEqual(self.ledger().ticket_open("names-unit")[0]["kind"], "read")

    def test_decision_refill_waives_a_finding_whose_file_is_gone_so_the_decision_can_close(self) -> None:
        rule = self.rule("read")
        self.sync()
        issue = self.approved_decision(rule, self.sources[:2], "rename")
        gone = next(finding for finding in self.ledger().proposal_findings(issue) if finding.path == self.sources[1])
        _ = self.git("rm", "-q", self.sources[1])
        self.write(self.sources[0], "pub enum Outcome { New }\n")
        self.commit_to_every_checkout()
        with mock.patch.object(style_proposals, "proposal_command"):
            code, output, errors = self.run_next()
        self.assertEqual(code, 0, errors)
        self.assertIn(f"waived 1 findings in {issue}: {self.sources[1]} is not in synced commit {self.tip}\n", output)
        ledger = self.ledger()
        self.assertIsInstance(next(finding.state for finding in ledger.proposal_findings(issue)
                                   if finding.finding_id == gone.finding_id), style_ledger.Waived)
        with closing(sqlite3.connect(ledger.database)) as connection:
            self.assertEqual(connection.execute("SELECT waive_provenance,waived_by FROM held_findings WHERE finding_id=?",
                                                (gone.finding_id,)).fetchone(), ("ledger", "ledger"))
        self.assertEqual(ledger.named_held((gone.finding_id,))[0].waive_actor, style_ledger.WaiveByLedger())
        ticket = ledger.ticket_open("names-unit")[0]
        self.assertEqual((ticket["kind"], ticket["files"]), ("proposal", [self.sources[0]]))
        ledger.ticket_checkpoint(ticket["id"], self.tip)
        _ = ledger.ticket_merge(ticket["id"])
        with (mock.patch.object(style_proposals, "proposal_command") as command,
              mock.patch.object(style_proposals, "renumber")):
            style_proposals.complete(ledger)
        command.assert_called_once_with("close", str(issue), "--reason", f"built in {ticket['id']}")

if __name__ == "__main__":
    _ = unittest.main()
