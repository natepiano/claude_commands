"""Refill shares immutable source evidence and invalidates mutable ledger evidence."""

from __future__ import annotations

import io
import sys
from collections.abc import Callable
from contextlib import redirect_stdout
import unittest
from pathlib import Path
from typing import cast, final
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import declarations
import approved_renames
import style_footprint
import style_ledger
import style_plan
import style_proposals
import rules
from unit_plans import AllCrates, PilotCrates
from style_plan_test_support import StylePlanFixture
from ledger_test_support import LedgerTestCase, PARENT_FILE


@final
class RefillSpeedTests(LedgerTestCase):
    def test_distinct_item_bodies_share_source_tokens(self) -> None:
        source = "fn first() { one(); }\nfn second() { two(); }\n"
        file = declarations.read_declarations(source)
        expected = [declarations.read_declarations(source).item_body(item) for item in file.declared_items]
        with mock.patch.object(style_footprint, "token_spans", wraps=style_footprint.token_spans) as reads:
            actual = [file.item_body(item) for item in file.declared_items]
            self.assertEqual(actual, expected)
            self.assertEqual(reads.call_count, 1)

    def test_rank_exemptions_share_binding_scan(self) -> None:
        ledger, findings = self.sites()
        reader = style_proposals.RankExemptions(ledger, ledger.synced_commit())
        source = reader.source(PARENT_FILE)
        expected: list[bool] = []
        for finding in findings:
            rename = approved_renames.binding_rename(finding.kind, finding.before, finding.after, finding.line, finding.text)
            self.assertIsInstance(rename, approved_renames.BindingRename)
            if isinstance(rename, approved_renames.BindingRename):
                expected.append(isinstance(approved_renames.binding_scope(source, rename), approved_renames.BindingScope))
        with mock.patch.object(approved_renames, "_BindingScan", wraps=getattr(approved_renames, "_BindingScan")) as scans:
            self.assertEqual([reader.allows(finding, "names") for finding in findings], expected)
            self.assertEqual(scans.call_count, 1)

    def sites(self) -> tuple[style_ledger.Ledger, tuple[style_ledger.HeldFinding, ...]]:
        source = "fn work() {\n    let old = 791;\n    let second = 792;\n    let third = 793;\n}\n"
        ledger, rule, first = self.site_fixture(source=source)
        others = tuple(ledger.hold(rule, PARENT_FILE, line, source.splitlines()[line - 1], "rename", name,
                                   "fresh", first.rule_blob, first.ticket)
                       for line, name in ((3, "second"), (4, "third")))
        return ledger, (first, *others)

    def test_single_site_reads_load_built_findings_once(self) -> None:
        ledger, findings = self.sites()
        _ = self.built_finding(ledger, self.sources[1], "value", "renamed")
        tip = ledger.synced_commit()
        with mock.patch.object(ledger, "held_findings", wraps=ledger.held_findings) as reads:
            for finding in findings:
                self.assertIsInstance(ledger.finding_site(finding, tip), style_ledger.UnchangedSite)
            self.assertEqual(reads.call_args_list.count(mock.call(style_ledger.HeldFindingsInState("built"))), 1)

    def test_batched_sites_match_independent_single_reads(self) -> None:
        ledger, findings = self.sites()
        source = (self.repo / PARENT_FILE).read_text()
        tip = self.commit_tip(ledger, {PARENT_FILE: "// header\n" + source.replace("second = 792", "second = 999")})
        with style_ledger.Ledger(self.repo, ledger.database) as singles:
            expected = {finding.finding_id: singles.finding_site(finding, tip) for finding in findings}
        with mock.patch.object(ledger, "_declaration_reading", wraps=getattr(ledger, "_declaration_reading")) as reads:
            actual = ledger.finding_sites(findings, tip)
            self.assertEqual(actual, expected)
            self.assertEqual(reads.call_count, 1)
            self.assertEqual(ledger.finding_sites(findings, tip), expected)
            self.assertEqual(reads.call_count, 1)
        self.assertTrue(any(isinstance(site, style_ledger.UnchangedSite) for site in actual.values()))
        self.assertTrue(any(isinstance(site, style_ledger.NeedsJudgment) for site in actual.values()))

    def test_built_cache_observes_local_and_external_writes(self) -> None:
        ledger, findings = self.sites()
        tip = ledger.synced_commit()
        with mock.patch.object(ledger, "held_findings", wraps=ledger.held_findings) as reads:
            _ = ledger.finding_sites(findings, tip)
            _ = ledger.decline_findings([findings[0].finding_id], "no longer needed", style_ledger.WaiveByLedger())
            _ = ledger.finding_sites(findings[1:], tip)
            with style_ledger.Ledger(self.repo, ledger.database) as other:
                _ = other.decline_findings([findings[1].finding_id], "no longer needed", style_ledger.WaiveByLedger())
            _ = ledger.finding_sites(findings[2:], tip)
            self.assertEqual(reads.call_args_list.count(mock.call(style_ledger.HeldFindingsInState("built"))), 3)
        current = ledger.named_held([finding.finding_id for finding in findings])
        evidence = ledger.finding_sites(current, tip)
        for finding in current[:2]:
            self.assertIsInstance(evidence[finding.finding_id], style_ledger.NeedsJudgment)

    def test_candidate_comparisons_read_each_item_body_once(self) -> None:
        ledger, _ = self.sites()
        old_tip = ledger.synced_commit()
        source = "fn first() {\n    use_value();\n}\nfn second() {\n    other_value();\n}\n"
        tip = self.commit_tip(ledger, {PARENT_FILE: source})
        declared = cast(Callable[[str, str], declarations.DeclaredFile], getattr(ledger, "_declared_file"))
        old = declared(old_tip, PARENT_FILE).declared_items[0]
        candidates = declared(tip, PARENT_FILE).declared_items
        check = cast(Callable[[str, str, declarations.DeclaredItem, str, str, declarations.DeclaredItem], object],
                     getattr(ledger, "_outside_body_check"))
        with mock.patch.object(declarations, "item_body", wraps=declarations.item_body) as reads:
            for item in candidates:
                _ = check(old_tip, PARENT_FILE, old, tip, PARENT_FILE, item)
            self.assertEqual(reads.call_count, 1 + len(candidates))


@final
class RefillWaitTests(StylePlanFixture):
    def test_blocked_files_need_no_extra_site_proof(self) -> None:
        rule = self.rule("blocked-sites")
        self.sync()
        issue = self.approved_decision(rule, (self.sources[0],), "type")
        ledger = self.ledger()
        findings = ledger.proposal_findings(issue)
        reading = style_ledger.DeclarationReading(self.tip, tuple(style_ledger.DeclarationNotRead(finding) for finding in findings))
        definitions = {rule.rule_id: rule for rule in rules.load_rules(self.repo)}
        for reason in ("busy", "outside", "unsynced"):
            with self.subTest(reason=reason):
                scope = PilotCrates(frozenset({"crates/beta"})) if reason == "outside" else AllCrates()
                busy = style_plan.BusyFiles(self.board(), frozenset({self.sources[0]}) if reason == "busy" else frozenset())
                unsynced = {self.sources[0]: style_plan.UnsyncedChange(self.sources[0], self.tip)} if reason == "unsynced" else {}
                with (mock.patch.object(style_proposals, "run_follow", return_value=style_proposals.FollowRun(reading, (), ())),
                      mock.patch.object(style_proposals, "ranked_work", return_value=style_proposals.RankedWork({})),
                      mock.patch.object(style_proposals, "ranked_below_unit", return_value=True),
                      mock.patch.object(style_plan, "unsynced_changes", return_value=unsynced),
                      mock.patch.object(ledger, "_declaration_reading", side_effect=AssertionError("blocked file requested site proof")),
                      redirect_stdout(io.StringIO())):
                    result = style_plan._select_phase(ledger, "names-unit", "demo-style-names", self.plan, self.repo,  # pyright: ignore[reportPrivateUsage]
                        self.doc, self.plan.read_text(), scope, busy, frozenset(), definitions,
                        style_plan.MainChanges(self.tip), family="names", selection=style_plan.FamilySelection.COUNT)
                self.assertIsInstance(result, style_plan.BuildableFindings)
                if isinstance(result, style_plan.BuildableFindings):
                    self.assertEqual(result.count, 0)
                self.assertEqual(ledger.proposal_findings(issue), findings)

    def test_changed_findings_share_one_file_wait(self) -> None:
        rule = self.rule("waiting-sites")
        _ = self.types_unit()
        source = self.sources[0]
        before = "unknown!(one);\nunknown!(two);\n"
        self.commit_files({source: before})
        issue, _ = self.judged_findings(rule, ((source, 1, "unknown!(one);"), (source, 2, "unknown!(two);")))
        self.commit_files({source: "unknown!(three);\nunknown!(four);\n"})
        output = self.refill_decisions()
        expected = f"waiting {issue}: {source} changed since it was judged (2 findings)"
        self.assertEqual(output.count(expected), 1)
        self.assertEqual(len(self.ledger().proposal_findings(issue)), 2)


if __name__ == "__main__":
    _ = unittest.main()
