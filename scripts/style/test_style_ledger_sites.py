"""Declaration sites, trait identity, and rename following."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import unittest
from collections.abc import Callable
from pathlib import Path
from contextlib import closing
from typing import cast, final
from unittest import mock
import style_ledger
from approved_renames import AppliedInCode, BindingRename, RenameNotApplied, binding_rename_check
import declarations
import style_footprint

from ledger_test_support import (LedgerTestCase, PARENT_FILE, MOVED_FILE, TWIN_FILE, PARENT, TWIN, TRAIT_METHOD)


# Hang guards allow for load 136 on natedev (2026-10-29).
TEST_HANG_GUARD_SECS = 300


@final
class StyleLedgerSitesTests(LedgerTestCase):
    def test_context_site_refuses_deleted_same_file_duplicate_in_original_crate_scope(self) -> None:
        source = "fn first() {\n    consume(791);\n}\nfn duplicate() {\n    consume(791);\n}\n"
        ledger, _, finding = self.site_fixture("types", rule_name="no-duplicate-implementations",
                                              scope="crate", source=source, before="791", kind="type")
        tip = self.commit_tip(ledger, {PARENT_FILE: source[:source.index("fn duplicate")]})
        self.assertIsInstance(self.strict_site(ledger, finding, tip), style_ledger.NeedsJudgment)
        self.assertIsInstance(ledger.finding_site(finding, tip), style_ledger.NeedsJudgment)
        _ = ledger.follow_declarations([finding.finding_id], tip, lambda _: True)
        self.assertEqual(ledger.named_held([finding.finding_id])[0].follow, style_ledger.NotFollowed())


    def test_site_owner_refuses_nested_function_moved_between_enclosing_functions(self) -> None:
        source = "fn first() {\n    fn work() {\n        let old = 791;\n    }\n}\nfn second() {\n}\n"
        ledger, _, finding = self.site_fixture(source=source, line=3)
        moved = "fn first() {\n}\nfn second() {\n    fn work() {\n        let old = 791;\n    }\n}\n"
        tip = self.commit_tip(ledger, {PARENT_FILE: moved})
        self.assertIsInstance(self.strict_site(ledger, finding, tip), style_ledger.NeedsJudgment)
        self.assertIsInstance(ledger.finding_site(finding, tip), style_ledger.NeedsJudgment)


    def test_site_owner_refuses_work_moved_between_nested_extern_functions(self) -> None:
        self.assert_extern_owner_move_refused("fn")


    def test_site_owner_refuses_qualified_work_moved_between_nested_extern_functions(self) -> None:
        self.assert_extern_owner_move_refused('pub(crate) const unsafe extern "C" fn')


    def test_site_owner_refuses_unknown_item_prefix_instead_of_skipping_it(self) -> None:
        source = "fn container() {\n    unrecognized fn first() {\n        fn work() {\n            let old = 791;\n        }\n    }\n}\n"
        ledger, _, finding = self.site_fixture(source=source, line=4)
        tip = self.commit_tip(ledger, {PARENT_FILE: "// header\n" + source})
        self.assertIsInstance(self.strict_site(ledger, finding, tip), style_ledger.NeedsJudgment)
        self.assertIsInstance(ledger.finding_site(finding, tip), style_ledger.NeedsJudgment)


    def test_site_proof_never_withdraws_removed_or_indirectly_included_literals(self) -> None:
        ledger, rule, finding = self.site_fixture("values", rule_name="no-magic-values", kind="restructure")
        judgments = self.judgment_rows(ledger.database)
        for expansion in ('use std::include as load; load!("body.inc");',
                          '#[cfg_attr(all(), path = "body.inc")] mod body;', ''):
            with self.subTest(expansion=expansion):
                tip = self.commit_tip(ledger, {PARENT_FILE: "fn work() {}\n",
                                               MOVED_FILE: expansion + "\n",
                                               "crates/alpha/src/body.inc": "fn included() { consume(791); }\n"})
                self.assertIsInstance(self.strict_site(ledger, finding, tip), style_ledger.NeedsJudgment)
                self.assertIsInstance(ledger.finding_site(finding, tip), style_ledger.NeedsJudgment)
                _ = ledger.follow_declarations([finding.finding_id], tip, lambda _: True)
                self.assertEqual(ledger.named_held([finding.finding_id])[0], finding)
                self.assertEqual(self.judgment_rows(ledger.database), judgments)
                self.assertEqual(self.states(rule)[PARENT_FILE], "stale")


    def test_site_owner_ignores_function_types_and_inline_const_expressions(self) -> None:
        source = "fn work(callback: fn()) {\n    let old = 791;\n    let value = const { 1 };\n}\n"
        ledger, rule, finding = self.site_fixture(source=source)
        tip = self.commit_tip(ledger, {PARENT_FILE: "// header comment\n" + source})
        self.assertEqual(self.strict_site(ledger, finding, tip), style_ledger.UnchangedSite(PARENT_FILE, 3))
        self.assertEqual(ledger.finding_site(finding, tip), style_ledger.UnchangedSite(PARENT_FILE, 3))
        judgments = self.judgment_rows(ledger.database)
        _ = ledger.follow_declarations([finding.finding_id], tip, lambda _: True)
        followed = ledger.named_held([finding.finding_id])[0]
        self.assertEqual(followed.follow, style_ledger.FollowedDeclaration(PARENT_FILE, 2, tip))
        self.assertEqual(followed.line, 3)
        self.assertEqual(self.judgment_rows(ledger.database), judgments)
        self.assertEqual(self.states(rule)[PARENT_FILE], "stale")


    def test_site_proof_refuses_unicode_token_failure_instead_of_empty_parse(self) -> None:
        source = "fn café() {\n    let old = 791;\n}\n"
        ledger, _, finding = self.site_fixture(source=source)
        tip = self.commit_tip(ledger, {PARENT_FILE: "// shift\n" + source})
        self.assertIsInstance(self.strict_site(ledger, finding, tip), style_ledger.NeedsJudgment)
        self.assertIsInstance(ledger.finding_site(finding, tip), style_ledger.NeedsJudgment)
        with self.assertRaises(style_footprint.UnreadableTokenSpans):
            _ = style_footprint.token_spans(source)
        self.assertEqual(style_footprint.token_spans("// no code\n"), ())


    def test_site_follow_preserves_line_evidence_and_pinned_fields_without_restamping_cells(self) -> None:
        ledger, rule, finding = self.site_fixture()
        # Keep the historical rule readable when both original pins are validated.
        _ = subprocess.run(["git", "init", "-q", str(self.rule_dir.parent)], check=True)
        _ = subprocess.run(["git", "-C", str(self.rule_dir.parent), "hash-object", "-w",
                            str(self.rule_dir / "site-rule.md")], check=True, capture_output=True)
        # A substantive rule edit must remain separate coverage work after the follow.
        _ = (self.rule_dir / "site-rule.md").write_text((self.rule_dir / "site-rule.md").read_text() + "New requirement\n")
        tip = self.commit_tip(ledger, {PARENT_FILE: "// inserted\nfn work() {\n    let old = 791;\n}\nfn sibling() {}\n"})
        judgments = self.judgment_rows(ledger.database)
        _ = ledger.follow_declarations([finding.finding_id], tip, lambda _: True)
        followed = ledger.named_held([finding.finding_id])[0]
        self.assertEqual((followed.finding_id, followed.rule_blob, followed.before, followed.after, followed.state),
                         (finding.finding_id, finding.rule_blob, finding.before, finding.after, finding.state))
        self.assertEqual((followed.path, followed.line), (PARENT_FILE, 3))
        self.assertEqual(followed.follow, style_ledger.FollowedDeclaration(PARENT_FILE, 2, tip))
        self.assertEqual(self.judgment_rows(ledger.database), judgments)
        self.assertEqual(self.states(rule)[PARENT_FILE], "stale")
        rows = self.held_rows(ledger.database)
        _ = ledger.follow_declarations([finding.finding_id], tip, lambda _: True)
        self.assertEqual(self.held_rows(ledger.database), rows)


    def test_site_follow_requires_exact_line_and_unambiguous_owner(self) -> None:
        ledger, _, finding = self.site_fixture()
        for source in ("fn renamed() {\n    let old = 791;\n}\n",
                       "fn work() {\n    let old = 791;\n    let old = 791;\n}\n",
                       "fn work() {\n    let old =\n        791;\n}\n",
                       "fn work() {}\nfn moved() {\n    let old = 791;\n}\n"):
            with self.subTest(source=source):
                tip = self.commit_tip(ledger, {PARENT_FILE: source})
                self.assertIsInstance(self.strict_site(ledger, finding, tip), style_ledger.NeedsJudgment)


    def test_context_site_follows_whole_function_but_rejects_body_attributes_imports_and_owner_changes(self) -> None:
        original = "use crate::Other;\nstruct Parent;\nimpl Parent {\n    #[inline]\n    fn work() {\n        consume(791);\n    }\n}\n"
        ledger, _, finding = self.site_fixture("types", source=original, line=6, before="791", kind="type")
        tip = self.commit_tip(ledger, {PARENT_FILE: original + "fn sibling() {}\n"})
        self.assertEqual(self.strict_site(ledger, finding, tip), style_ledger.UnchangedSite(PARENT_FILE, 6))
        for source in (original.replace("consume(791);", "consume(791);\n        other();"),
                       original.replace("#[inline]", "#[cold]"), original.replace("crate::Other", "crate::Changed"),
                       original.replace("impl Parent", "impl Other")):
            with self.subTest(source=source):
                tip = self.commit_tip(ledger, {PARENT_FILE: source})
                self.assertIsInstance(self.strict_site(ledger, finding, tip), style_ledger.NeedsJudgment)


    def test_context_site_proves_complete_impl_including_its_attributes_and_body(self) -> None:
        source = "struct Parent;\n#[allow(dead_code)]\nimpl Parent {\n    fn work() { consume(791); }\n}\n"
        ledger, _, finding = self.site_fixture("types", source=source, line=3, kind="type", before="impl Parent")
        tip = self.commit_tip(ledger, {PARENT_FILE: source + "fn sibling() {}\n"})
        self.assertEqual(self.strict_site(ledger, finding, tip), style_ledger.UnchangedSite(PARENT_FILE, 3))
        tip = self.commit_tip(ledger, {PARENT_FILE: source.replace("consume(791)", "consume(792)")})
        self.assertIsInstance(self.strict_site(ledger, finding, tip), style_ledger.NeedsJudgment)
        tip = self.commit_tip(ledger, {PARENT_FILE: source.replace("allow(dead_code)", "deny(dead_code)")})
        self.assertIsInstance(self.strict_site(ledger, finding, tip), style_ledger.NeedsJudgment)


    def test_site_owner_includes_inline_module_identity(self) -> None:
        source = "mod first {\n    fn work() {\n        let old = 791;\n    }\n}\n"
        ledger, _, finding = self.site_fixture(source=source, line=3)
        tip = self.commit_tip(ledger, {PARENT_FILE: source.replace("mod first", "mod renamed")})
        self.assertIsInstance(self.strict_site(ledger, finding, tip), style_ledger.NeedsJudgment)


    def test_site_follow_requires_byte_identical_line_endings(self) -> None:
        source = "fn work() {\r\n    let old = 791;\r\n}\r\n"
        ledger, _, finding = self.site_fixture(source=source)
        tip = self.commit_tip(ledger, {PARENT_FILE: source.replace("\r\n", "\n")})
        self.assertIsInstance(self.strict_site(ledger, finding, tip), style_ledger.NeedsJudgment)


    def test_context_site_keeps_original_crate_scope_when_rule_narrows_and_after_prior_follow(self) -> None:
        ledger, _, finding = self.site_fixture("types", scope="crate", kind="type")
        source = (self.repo / PARENT_FILE).read_text()
        tip = self.commit_tip(ledger, {PARENT_FILE: "fn sibling() {}\n" + source})
        self.assertIsInstance(self.strict_site(ledger, finding, tip), style_ledger.NeedsJudgment)
        self.assertIsInstance(ledger.finding_site(finding, tip), style_ledger.NeedsJudgment)
        tip = self.commit_tip(ledger, {PARENT_FILE: "fn sibling() {}\n", MOVED_FILE: "// moved\n" + source})
        outcomes = ledger.follow_declarations([finding.finding_id], tip, lambda _: True)
        self.assertIsInstance(outcomes.outcomes[0], style_ledger.DeclarationMoved)
        followed = ledger.named_held([finding.finding_id])[0]
        self.assertEqual(followed.line, 3)
        self.assertEqual(followed.path, MOVED_FILE)
        self.assertEqual(followed.follow, style_ledger.FollowedDeclaration(PARENT_FILE, 2, tip))
        _ = (self.rule_dir / "site-rule.md").write_text("---\ntier: C\nfamily: types\ninput_scope: file\n---\n# Narrowed\n")
        tip = self.commit_tip(ledger, {self.sources[1]: "fn changed_dependency() {}\n"})
        self.assertIsInstance(self.strict_site(ledger, followed, tip), style_ledger.NeedsJudgment)


    def test_context_site_rejects_changed_explicit_reads(self) -> None:
        ledger, rule, finding = self.site_fixture("modules", kind="restructure", scope="crate")
        self.record_reads(rule, PARENT_FILE, ledger.synced_commit(), (self.sources[1],), verdict="held", ticket="judge-1")
        tip = self.commit_tip(ledger, {PARENT_FILE: "fn sibling() {}\n" + (self.repo / PARENT_FILE).read_text(),
                                       self.sources[1]: "fn changed_dependency() {}\n"})
        self.assertIsInstance(self.strict_site(ledger, finding, tip), style_ledger.NeedsJudgment)


    def test_site_follow_rejects_late_pre_deletion_judgment_after_identical_restore(self) -> None:
        ledger, rule, finding = self.site_fixture()
        source = (self.repo / PARENT_FILE).read_text()
        before = ledger.synced_commit()
        deletion = self.commit_tip(ledger, {PARENT_FILE: None})
        with sqlite3.connect(ledger.database) as connection:
            _ = connection.execute('INSERT INTO judgments(rule,rule_blob,input_fingerprint,path,"commit",verdict,verdict_detail,ticket,worker,recorded_at,input_scope,declared_scope) '
                                   + "VALUES (?,?, '',?,?,'removed','','removal','test','2000','file','rule')",
                                   (rule, self.blob(rule), PARENT_FILE, deletion))
        tip = self.commit_tip(ledger, {PARENT_FILE: source})
        # Recorded after restoration, but still judges the pre-deletion file.
        ledger.record(rule, PARENT_FILE, before, self.blob(rule), "held", ticket="judge-1", worker="late",
                      input_scope=style_ledger.DeclaredInputScope.RULE)
        self.assertIsInstance(self.strict_site(ledger, finding, tip), style_ledger.NeedsJudgment)
        _ = ledger.follow_declarations([finding.finding_id], tip, lambda _: True)
        self.assertEqual(ledger.named_held([finding.finding_id])[0].follow, style_ledger.NotFollowed())


    def test_site_follow_rechecks_rows_under_lock_and_never_overwrites_new_text(self) -> None:
        ledger, _, finding = self.site_fixture()
        tip = self.commit_tip(ledger, {PARENT_FILE: "// shift\n" + (self.repo / PARENT_FILE).read_text()})
        original = cast(Callable[[tuple[str, ...], style_ledger.RepositoryTip, Callable[[Path], bool]], style_ledger.DeclarationReading],
                        getattr(ledger, "_declaration_reading"))
        raced = False

        def racing_read(ids: tuple[str, ...], snapshot: style_ledger.RepositoryTip,
                        approved: Callable[[Path], bool]) -> style_ledger.DeclarationReading:
            nonlocal raced
            reading = original(ids, snapshot, approved)
            if not raced:
                raced = True
                with sqlite3.connect(ledger.database) as connection:
                    _ = connection.execute("UPDATE held_findings SET text='changed by another writer' WHERE finding_id=?", (finding.finding_id,))
            return reading

        with mock.patch.object(ledger, "_declaration_reading", side_effect=racing_read):
            _ = ledger.follow_declarations([finding.finding_id], tip, lambda _: True)
        current = ledger.named_held([finding.finding_id])[0]
        self.assertEqual((current.text, current.follow), ("changed by another writer", style_ledger.NotFollowed()))


    def test_site_follow_refuses_tip_race_before_writing(self) -> None:
        ledger, _, finding = self.site_fixture()
        tip = self.commit_tip(ledger, {PARENT_FILE: "// shift\n" + (self.repo / PARENT_FILE).read_text()})
        original = cast(Callable[[tuple[str, ...], style_ledger.RepositoryTip, Callable[[Path], bool]], style_ledger.DeclarationReading],
                        getattr(ledger, "_declaration_reading"))
        raced = False

        def racing_read(ids: tuple[str, ...], snapshot: style_ledger.RepositoryTip,
                        approved: Callable[[Path], bool]) -> style_ledger.DeclarationReading:
            nonlocal raced
            reading = original(ids, snapshot, approved)
            if not raced:
                raced = True
                _ = self.commit_tip(ledger, {MOVED_FILE: "fn new_tip() {}\n"})
            return reading

        with mock.patch.object(ledger, "_declaration_reading", side_effect=racing_read):
            with self.assertRaisesRegex(style_ledger.LedgerError, "synced tip moved"):
                _ = ledger.follow_declarations([finding.finding_id], tip, lambda _: True)
        self.assertEqual(ledger.named_held([finding.finding_id])[0].follow, style_ledger.NotFollowed())


    def test_site_proof_requires_a_type_proposals_before_subject_in_its_item(self) -> None:
        ledger, _, finding = self.site_fixture(kind="type", before="unlocated subject")
        tip = self.commit_tip(ledger, {PARENT_FILE: "// shift\n" + (self.repo / PARENT_FILE).read_text()})
        self.assertIsInstance(ledger.finding_site(finding, tip), style_ledger.NeedsJudgment)


    def test_site_proof_rejects_partly_unreadable_item_parse(self) -> None:
        ledger, _, finding = self.site_fixture()
        source = (self.repo / PARENT_FILE).read_text()
        tip = self.commit_tip(ledger, {PARENT_FILE: source + "fn sibling() {}\n"})
        self.assertIsInstance(ledger.finding_site(finding, tip), style_ledger.UnchangedSite)
        tip = self.commit_tip(ledger, {PARENT_FILE: source + "mod nested { unsupported; }\n"})
        self.assertIsInstance(ledger.finding_site(finding, tip), style_ledger.NeedsJudgment)
        _ = ledger.follow_declarations([finding.finding_id], tip, lambda _: True)
        self.assertEqual(ledger.named_held([finding.finding_id])[0].follow, style_ledger.NotFollowed())


    def test_site_proof_rejects_non_utf8_pinned_rule_text(self) -> None:
        ledger, _, finding = self.site_fixture()
        tip = self.commit_tip(ledger, {PARENT_FILE: "// shift\n" + (self.repo / PARENT_FILE).read_text()})
        self.assertIsInstance(ledger.finding_site(finding, tip), style_ledger.UnchangedSite)
        with sqlite3.connect(ledger.database) as connection:
            _ = connection.execute("UPDATE rule_texts SET text=? WHERE blob=?", (b"family: names\n\xff", finding.rule_blob))
        self.assertIsInstance(ledger.finding_site(finding, tip), style_ledger.NeedsJudgment)


    def test_site_proof_rejects_non_utf8_source(self) -> None:
        ledger, _, finding = self.site_fixture()
        source = (self.repo / PARENT_FILE).read_bytes()
        _ = (self.repo / PARENT_FILE).write_bytes(source + b"// unreadable: \xff\n")
        tip = self.commit("Non-UTF-8 source")
        ledger.set_tip(tip)
        self.assertIsInstance(ledger.finding_site(finding, tip), style_ledger.NeedsJudgment)
        _ = ledger.follow_declarations([finding.finding_id], tip, lambda _: True)
        self.assertEqual(ledger.named_held([finding.finding_id])[0].follow, style_ledger.NotFollowed())


    def test_binding_carry_restamps_types_and_follows_open_sites_through_reflow(self) -> None:
        source = "fn f() {\n    let old = input;\n    consume(old);\n}\n"
        ledger, _, base = self.binding_ticket(source, ((2, "old", "fresh"),))
        path = self.sources[0]
        types = self.rule("types-read", "tier: C\nfamily: types\ninput_scope: file\n")
        # The types read predates the rename ticket but reads exactly the base blob.
        ledger.ticket_add("types-1", "types", "read", "types", "crates/alpha", [path], [types])
        ledger.ticket_assign("types-1", "main")
        _ = ledger.ticket_base("types-1")
        ledger.ticket_drop("types-1")
        findings = tuple(ledger.hold(types, path, line, source.splitlines()[line - 1], "type", "u32", "Count",
                                     self.blob(types), "types-1") for line in (2, 3))
        ledger.file_held(findings[1].finding_id, self.root / "types.md")
        ledger.record(types, path, base, self.blob(types), "held", ticket="types-1", worker="judge",
                      input_scope=style_ledger.DeclaredInputScope.RULE)
        crate_types = self.rule("crate-types", "tier: C\nfamily: types\ninput_scope: crate\n")
        reads_types = self.rule("reads-types", "tier: C\nfamily: types\ninput_scope: crate\n")
        for rule in (crate_types, reads_types):
            ledger.record(rule, path, base, self.blob(rule), "clean", ticket="outside", worker="judge",
                          input_scope=style_ledger.DeclaredInputScope.RULE,
                          reads=(self.sources[1],) if rule == reads_types else style_ledger.NO_READ_FILES)
        self.write(path, source.replace("old", "fresh").replace("    let", "\n    let"))
        checkpoint = self.commit("Rename and reflow")
        ledger.set_tip(checkpoint, "main")
        ledger.ticket_checkpoint("names-1", checkpoint)
        self.assertTrue(all(isinstance(f.follow, style_ledger.NotFollowed) for f in ledger.named_held([f.finding_id for f in findings])))
        _ = ledger.ticket_merge("names-1")
        moved = tuple(sorted(ledger.named_held([f.finding_id for f in findings]), key=lambda f: f.line))
        self.assertEqual([f.line for f in moved], [3, 4])
        self.assertEqual([f.text for f in moved], ["    let fresh = input;", "    consume(fresh);"])
        self.assertEqual([f.follow for f in moved], [style_ledger.FollowedDeclaration(path, line, checkpoint) for line in (2, 3)])
        self.assertEqual([style_ledger.held_state_name(f.state) for f in moved], ["unfiled", "filed"])
        self.assertEqual([f.ticket for f in moved], ["types-1", "types-1"])
        with closing(sqlite3.connect(ledger.database)) as connection:
            rows = connection.execute('SELECT "commit",ticket,worker,verdict FROM judgments WHERE rule=? ORDER BY id', (types,)).fetchall()
        self.assertEqual(rows, [(base, "types-1", "judge", "held"), (checkpoint, "types-1", "judge", "held")])
        with closing(sqlite3.connect(ledger.database)) as connection:
            carried_rules = connection.execute('SELECT rule FROM judgments WHERE "commit"=? ORDER BY rule', (checkpoint,)).fetchall()
        self.assertEqual(carried_rules, [(rule,) for rule in sorted((types, crate_types, reads_types))])
        self.assertEqual(next(ledger.cells(rule=types)).state, "current")
        # A repeated merge cannot restamp or follow twice.
        _ = ledger.ticket_merge("names-1")
        self.assertEqual(tuple(sorted(ledger.named_held([f.finding_id for f in findings]), key=lambda f: f.line)), moved)


    def test_binding_carry_rejects_a_late_judgment_before_deletion_and_identical_restore(self) -> None:
        source = "fn f() {\n    let old = input;\n    consume(old);\n}\n"
        path = self.sources[0]
        types = self.rule("types-read", "tier: C\nfamily: types\ninput_scope: file\n")
        self.write(path, source)
        original = self.commit("Original binding before deletion")
        ledger = self.ledger()
        ledger.ticket_add("types-1", "types", "read", "types", "crates/alpha", [path], [types])
        ledger.ticket_assign("types-1", "main")
        self.assertEqual(ledger.ticket_base("types-1"), original)
        (self.repo / path).unlink()
        deleting = self.commit("Delete the types cell")
        self.record_removed(ledger, types, path, deleting, ticket="types-1")
        # The rename ticket starts at a byte-identical restoration of the deleted file.
        ledger, _, base = self.binding_ticket(source, ((2, "old", "fresh"),))
        self.assertEqual(self.git("rev-parse", f"{original}:{path}"), self.git("rev-parse", f"{base}:{path}"))
        ledger.record(types, path, original, self.blob(types), "clean", ticket="types-1", worker="late-judge",
                      input_scope=style_ledger.DeclaredInputScope.RULE)
        ledger.set_tip(base, "main")
        self.assertEqual(next(ledger.cells(rule=types)).state, "open")
        self.write(path, source.replace("old", "fresh"))
        checkpoint = self.commit("Proven binding rename after restoration")
        ledger.set_tip(checkpoint, "main")
        ledger.ticket_checkpoint("names-1", checkpoint)
        ticket, = ledger.proposal_tickets()
        self.assertEqual(ledger.binding_carry_forward(ticket, checkpoint), style_ledger.BindingCarryForward())
        self.assertEqual(ledger.ticket_merge("names-1"), ())
        self.assertEqual(self.cell_rows(ledger, path), [(deleting, "removed"), (original, "clean")])
        self.assertEqual(next(ledger.cells(rule=types)).state, "open")


    def test_binding_carry_requires_whole_file_proof_current_rule_and_unchanged_other_inputs(self) -> None:
        source = "fn f() {\n    let old = input;\n    consume(old);\n}\n"
        ledger, _, base = self.binding_ticket(source, ((2, "old", "fresh"),))
        path, other = self.sources[:2]
        carried = self.rule("carried-types", "tier: C\nfamily: types\ninput_scope: file\n")
        reads = self.rule("reads-types", "tier: C\nfamily: types\ninput_scope: crate\n")
        crate = self.rule("crate-types", "tier: C\nfamily: types\ninput_scope: crate\n")
        changed = self.rule("changed-types", "tier: C\nfamily: types\ninput_scope: file\n")
        old_blob = self.rule("older-file-types", "tier: C\nfamily: types\ninput_scope: file\n")
        ledger.record(old_blob, path, self.initial_tip, self.blob(old_blob), "clean", ticket="types", worker="judge",
                      input_scope=style_ledger.DeclaredInputScope.RULE)
        for rule in (carried, crate, changed):
            ledger.record(rule, path, base, self.blob(rule), "clean", ticket="types", worker="judge",
                          input_scope=style_ledger.DeclaredInputScope.RULE)
        ledger.record(reads, path, base, self.blob(reads), "clean", ticket="types", worker="judge",
                      input_scope=style_ledger.DeclaredInputScope.RULE, reads=(other,))
        _ = self.rule("changed-types", "tier: C\nfamily: types\ninput_scope: file\nmode: propose\n")
        self.write(path, source.replace("old", "fresh"))
        self.write(other, "fn another_change() {}\n")
        checkpoint = self.commit("Rename plus another input changed")
        ledger.set_tip(checkpoint, "main")
        ledger.ticket_checkpoint("names-1", checkpoint)
        _ = ledger.ticket_merge("names-1")
        with closing(sqlite3.connect(ledger.database)) as connection:
            restamped = connection.execute('SELECT rule FROM judgments WHERE "commit"=?', (checkpoint,)).fetchall()
        self.assertEqual(restamped, [(carried,)])
        # A proof that applies one rename but cannot explain another token never carries types work.
        # Use the same public planning operation on a fresh checkpoint; it must refuse the whole path.
        self.write(path, source.replace("old", "fresh").replace("input", "different"))
        unproved = self.commit("Unexplained token change")
        ticket = ledger.proposal_tickets()[0]
        self.assertEqual(ledger.binding_carry_forward(ticket, unproved), style_ledger.BindingCarryForward())


    def test_binding_applied_check_groups_the_ticket_path_and_caches_each_commit(self) -> None:
        source = "fn f() {\n    let old = input;\n    let second = old;\n    consume(second);\n}\n"
        ledger, issue, base = self.binding_ticket(source, ((2, "old", "fresh"), (3, "second", "next")))
        findings = ledger.proposal_findings(issue)
        self.write(self.sources[0], source.replace("old", "fresh").replace("second", "next"))
        checkpoint = self.commit("Rename both bindings")
        with mock.patch.object(style_ledger, "binding_rename_check", wraps=binding_rename_check) as proof:
            self.assertEqual([ledger.applied_check(finding, checkpoint) for finding in findings], [AppliedInCode()] * 2)
            self.assertEqual(proof.call_count, 1)
            self.assertEqual(len(cast(tuple[BindingRename, ...], proof.call_args.args[3])), 2)
            self.assertEqual([ledger.applied_check(finding, base) for finding in findings],
                             [RenameNotApplied(self.sources[0])] * 2)
            self.assertEqual(proof.call_count, 2)
        ledger.set_tip(checkpoint, "main")
        ledger.ticket_checkpoint("names-1", checkpoint)
        self.assertEqual(ledger.ticket_merge("names-1"), ())
        self.assertTrue(all(isinstance(f.state, style_ledger.Built) for f in ledger.proposal_findings(issue)))
        self.assertTrue(all(isinstance(f.reading, style_ledger.NotReadByPerson) for f in ledger.proposal_findings(issue)))
        # A fresh ledger must group built findings too.
        fresh = self.ledger()
        self.assertEqual([fresh.applied_check(f, checkpoint) for f in fresh.proposal_findings(issue)], [AppliedInCode()] * 2)


    def test_hold_after_a_follow_returns_the_followed_finding_only_for_the_same_change(self) -> None:
        ledger = self.ledger()
        other = "pub struct Other {\n    old: u32,\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: PARENT, MOVED_FILE: other})
        rule, (parent,) = self.judge_parent(ledger, ((2, "old: u32,"),))
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub fn other() {}\n", MOVED_FILE: other + "\n" + PARENT})
        followed, = self.follow_approved(ledger, [parent])
        self.assertEqual((type(followed), ledger.held_findings(style_ledger.AllHeldFindings())[0].line), (style_ledger.DeclarationMoved, 6))
        ledger.ticket_add("read-9", "read", "read", "names", "crates/alpha", [MOVED_FILE], [rule])
        ledger.ticket_assign("read-9", "main")
        # A read of b.rs holds a different change on the same text, then the followed change again.
        held_other = ledger.hold(rule, MOVED_FILE, 2, "old: u32,", "rename", "Other::old", "Other::new", self.blob(rule), "read-9")
        held_parent = ledger.hold(rule, MOVED_FILE, 6, "old: u32,", "rename", "Parent::old", "Parent::new", self.blob(rule), "read-9")
        self.assertNotEqual(held_other.finding_id, parent)
        self.assertEqual((held_other.before, held_other.line, held_other.state), ("Other::old", 2, style_ledger.Unfiled()))
        self.assertEqual((held_parent.finding_id, held_parent.line), (parent, 6))
        self.assertEqual(len(ledger.held_findings(style_ledger.AllHeldFindings())), 2)


    def test_a_removed_declaration_whose_twin_was_already_in_another_file_is_withdrawn(self) -> None:
        ledger = self.ledger()
        _ = self.commit_tip(ledger, {PARENT_FILE: PARENT, TWIN_FILE: TWIN})
        _, (parent,) = self.judge_parent(ledger, ((2, "old: u32,"),))
        tip = self.commit_tip(ledger, {PARENT_FILE: None})
        removed, = self.follow_approved(ledger, [parent])
        self.assertIsInstance(removed, style_ledger.DeclarationRemoved)
        finding, = ledger.held_findings(style_ledger.AllHeldFindings())
        self.assertEqual((finding.path, finding.state),
                         (PARENT_FILE, style_ledger.Withdrawn(self.root / "decision.md", style_ledger.DeclarationGone(tip))))


    def test_a_move_to_one_file_follows_when_a_changed_third_file_already_declared_a_twin(self) -> None:
        ledger = self.ledger()
        _ = self.commit_tip(ledger, {PARENT_FILE: PARENT, TWIN_FILE: TWIN})
        _, (parent,) = self.judge_parent(ledger, ((2, "old: u32,"),))
        tip = self.commit_tip(ledger, {PARENT_FILE: "pub fn other() {}\n", MOVED_FILE: PARENT, TWIN_FILE: TWIN + "fn helper() {}\n"})
        moved, = self.follow_approved(ledger, [parent])
        self.assertIsInstance(moved, style_ledger.DeclarationMoved)
        finding, = ledger.held_findings(style_ledger.AllHeldFindings())
        self.assertEqual((finding.path, finding.line, finding.follow),
                         (MOVED_FILE, 2, style_ledger.FollowedDeclaration(PARENT_FILE, 2, tip)))


    def test_a_statement_follows_its_whole_moved_function_to_the_one_line_that_holds_its_text(self) -> None:
        outcome = self.follow_statement({PARENT_FILE: "pub fn keep() {}\n",
                                         MOVED_FILE: "fn other() {}\n\nfn check() {\n    let bytes = [0];\n\n    let rgba = convert(2, 16);\n}\n"})
        self.assertIsInstance(outcome, style_ledger.DeclarationMoved)
        self.assertEqual((cast(style_ledger.DeclarationMoved, outcome).path, cast(style_ledger.DeclarationMoved, outcome).line),
                         (MOVED_FILE, 6))


    def test_a_statement_whose_text_is_twice_in_the_moved_function_is_left(self) -> None:
        outcome = self.follow_statement({PARENT_FILE: "pub fn keep() {}\n",
                                         MOVED_FILE: "fn check() {\n    let rgba = convert(2, 16);\n    let rgba = convert(2, 16);\n}\n"})
        self.assertEqual(outcome, style_ledger.StatementNotFollowed(
            outcome.finding, declarations.Declaration("fn", "check", declarations.ModuleItem()), MOVED_FILE, declarations.StatementRefusal.TEXT_REPEATED))


    def test_a_statement_whose_text_changed_in_the_moved_function_is_left(self) -> None:
        outcome = self.follow_statement({PARENT_FILE: "pub fn keep() {}\n",
                                         MOVED_FILE: "fn check() {\n    let rgba = convert(WIDTH, STRIDE);\n}\n"})
        self.assertEqual(outcome, style_ledger.StatementNotFollowed(
            outcome.finding, declarations.Declaration("fn", "check", declarations.ModuleItem()), MOVED_FILE, declarations.StatementRefusal.TEXT_NOT_FOUND))


    def test_a_statement_whose_text_is_only_in_a_function_nested_in_the_moved_function_is_left(self) -> None:
        outcome = self.follow_statement({PARENT_FILE: "pub fn keep() {}\n",
                                         MOVED_FILE: "fn check() {\n    fn inner() {\n        let rgba = convert(2, 16);\n    }\n}\n"})
        self.assertEqual(outcome, style_ledger.StatementNotFollowed(
            outcome.finding, declarations.Declaration("fn", "check", declarations.ModuleItem()), MOVED_FILE, declarations.StatementRefusal.TEXT_NOT_FOUND))


    def test_a_statement_whose_text_is_only_in_a_comment_or_string_of_the_moved_function_is_left(self) -> None:
        body = ("    // let rgba = convert(2, 16);\n    /* let rgba = convert(2, 16); */\n"
                + "    let note = \"let rgba = convert(2, 16);\";\n")
        outcome = self.follow_statement({PARENT_FILE: "pub fn keep() {}\n", MOVED_FILE: "fn check() {\n" + body + "}\n"})
        self.assertEqual(outcome, style_ledger.StatementNotFollowed(
            outcome.finding, declarations.Declaration("fn", "check", declarations.ModuleItem()), MOVED_FILE,
            declarations.StatementRefusal.TEXT_NOT_FOUND))


    def test_a_statement_follows_to_its_code_line_past_a_commented_copy(self) -> None:
        outcome = self.follow_statement({PARENT_FILE: "pub fn keep() {}\n",
                                         MOVED_FILE: "fn check() {\n    // let rgba = convert(2, 16);\n    let rgba = convert(2, 16);\n}\n"})
        self.assertEqual((type(outcome), cast(style_ledger.DeclarationMoved, outcome).line), (style_ledger.DeclarationMoved, 3))


    def test_a_statement_with_a_trailing_comment_follows_its_unchanged_moved_function(self) -> None:
        ledger = self.ledger()
        source = "fn check() {\n    let bytes = [0];\n    let rgba = convert(2, 16); // RGBA stride\n    let alpha = convert(3, 8); /* alpha */\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub fn keep() {}\n\n" + source})
        _, ids = self.judge_parent(ledger, ((5, "let rgba = convert(2, 16); // RGBA stride"),
                                            (6, "let alpha = convert(3, 8); /* alpha */")), kind="restructure")
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub fn keep() {}\n", MOVED_FILE: "fn other() {}\n\n" + source})
        outcomes = self.follow_approved(ledger, list(ids))
        self.assertEqual([(type(outcome), cast(style_ledger.DeclarationMoved, outcome).line) for outcome in outcomes],
                         [(style_ledger.DeclarationMoved, 5), (style_ledger.DeclarationMoved, 6)])


    def test_a_statement_whose_function_went_to_two_files_is_left(self) -> None:
        body = "fn check() {\n    let rgba = convert(2, 16);\n}\n"
        outcome = self.follow_statement({PARENT_FILE: "pub fn keep() {}\n", MOVED_FILE: body, TWIN_FILE: body})
        self.assertIsInstance(outcome, style_ledger.DeclaredInSeveralFiles)


    def test_the_declaration_step_reads_each_commit_and_file_once_and_its_recheck_sees_another_writer(self) -> None:
        ledger = self.ledger()
        _ = self.commit_tip(ledger, {PARENT_FILE: PARENT})
        _, (field, header) = self.judge_parent(ledger, ((2, "old: u32,"), (1, "pub struct Parent {")))
        tip = self.commit_tip(ledger, {PARENT_FILE: "pub fn other() {}\n", MOVED_FILE: PARENT})
        judged = style_ledger.Ledger.judged_commit
        written = False

        def judged_with_another_writer(self_: style_ledger.Ledger, finding: style_ledger.HeldFinding) -> style_ledger.JudgedCommit | style_ledger.NoJudgedCommit:
            nonlocal written
            if not written:
                written = True
                # Another writer moves the field finding's line after the plan read its row, before the write lock.
                with closing(sqlite3.connect(ledger.database)) as connection, connection:
                    _ = connection.execute("UPDATE held_findings SET line=3,text='}' WHERE finding_id=?", (field,))
            return judged(self_, finding)

        with (mock.patch.object(style_ledger, "read_declarations", wraps=declarations.read_declarations) as reads,
              mock.patch.object(subprocess, "run", wraps=subprocess.run) as runs,
              mock.patch.object(style_ledger.Ledger, "judged_commit", judged_with_another_writer)):
            outcomes = self.follow_approved(ledger, [field, header])
        resolves = [call for call in runs.call_args_list if "--quiet" in cast(tuple[list[str]], call.args)[0]]
        self.assertEqual([type(outcome) for outcome in outcomes], [style_ledger.DeclarationMoved] * 2)
        # One judged commit, and each of a.rs and b.rs at the judged commit and at the tip, across two findings and
        # four readings: a plan, its refused recheck, a plan and its recheck.
        self.assertEqual((len(resolves), reads.call_count), (1, 4))
        rows = {finding.finding_id: (finding.path, finding.line, finding.follow) for finding in ledger.held_findings(style_ledger.AllHeldFindings())}
        self.assertEqual(rows[field], (MOVED_FILE, 3, style_ledger.FollowedDeclaration(PARENT_FILE, 3, tip)))
        self.assertEqual(rows[header][:2], (MOVED_FILE, 1))


    def test_a_filed_rename_whose_after_name_its_file_now_declares_is_built_not_withdrawn(self) -> None:
        ledger = self.ledger()
        finding_id = self.renamed_parent(ledger)
        # A person read an older build; the new build clears that reading.
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE held_findings SET person_read_by='nate' WHERE finding_id=?", (finding_id,))
        # The renamed declaration is two lines lower; the built finding follows it there, as `built` prints it.
        tip = self.commit_tip(ledger, {PARENT_FILE: "use std::fmt;\n\n" + PARENT.replace("Parent", "Renamed")})
        filed = ledger.held_findings(style_ledger.AllHeldFindings())[0]
        renamed, = self.follow_approved(ledger, [finding_id])
        self.assertEqual(renamed, style_ledger.DeclarationRenamed(
            filed, declarations.Declaration("struct", "Parent", declarations.ModuleItem()),
            declarations.Declaration("struct", "Renamed", declarations.ModuleItem()), PARENT_FILE, 3))
        built, = ledger.held_findings(style_ledger.AllHeldFindings())
        self.assertEqual((built.path, built.line, built.state, built.reading, built.follow),
                         (PARENT_FILE, 3, style_ledger.Built(self.root / "decision.md"), style_ledger.NotReadByPerson(),
                          style_ledger.FollowedDeclaration(PARENT_FILE, 1, tip)))


    def test_a_filed_rename_whose_after_name_one_gaining_file_declares_is_built_and_followed_there(self) -> None:
        ledger = self.ledger()
        finding_id = self.renamed_parent(ledger)
        tip = self.commit_tip(ledger, {PARENT_FILE: "pub fn other() {}\n",
                                       MOVED_FILE: "use std::fmt;\n\n" + PARENT.replace("Parent", "Renamed")})
        renamed, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(renamed, style_ledger.DeclarationRenamed)
        built, = ledger.held_findings(style_ledger.AllHeldFindings())
        self.assertEqual((built.path, built.line, built.state, built.follow),
                         (MOVED_FILE, 3, style_ledger.Built(self.root / "decision.md"),
                          style_ledger.FollowedDeclaration(PARENT_FILE, 1, tip)))


    def test_a_filed_rename_of_an_impl_method_is_built_and_its_applied_check_passes(self) -> None:
        ledger = self.ledger()
        method = "pub struct Parent;\n\nimpl Parent {\n    pub fn old(&self) {}\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: method})
        _, (finding_id,) = self.judge_parent(ledger, ((4, "pub fn old(&self) {}"),))
        tip = self.commit_tip(ledger, {PARENT_FILE: method.replace("fn old", "fn new")})
        filed = ledger.held_findings(style_ledger.AllHeldFindings())[0]
        renamed, = self.follow_approved(ledger, [finding_id])
        owner = declarations.ImplOrTraitItem("Parent")
        self.assertEqual(renamed, style_ledger.DeclarationRenamed(
            filed, declarations.Declaration("fn", "old", owner), declarations.Declaration("fn", "new", owner), PARENT_FILE, 4))
        built, = ledger.held_findings(style_ledger.AllHeldFindings())
        self.assertEqual(built.state, style_ledger.Built(self.root / "decision.md"))
        self.assertEqual(ledger.applied_check(built, tip), AppliedInCode())


    def test_a_filed_finding_follows_a_proved_outside_name_without_changing_its_request(self) -> None:
        ledger = self.ledger()
        finding_id = self.renamed_parent(ledger)
        _ = self.commit_tip(ledger, {PARENT_FILE: PARENT.replace("Parent", "Other")})
        followed, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(followed, style_ledger.DeclarationMoved)
        finding, = ledger.held_findings(style_ledger.AllHeldFindings())
        self.assertEqual((finding.finding_id, finding.before, finding.after, finding.state),
                         (finding_id, "Parent", "Renamed", style_ledger.Filed(self.root / "decision.md")))
        before = self.held_rows(ledger.database)
        again, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(again, style_ledger.DeclarationInPlace)
        self.assertEqual(self.held_rows(ledger.database), before)


    def test_a_filed_finding_with_no_proved_destination_stays_filed_after_its_file_is_deleted(self) -> None:
        ledger = self.ledger()
        finding_id = self.renamed_parent(ledger)
        for contents in ("pub fn other() {}\n", None):
            with self.subTest(contents=contents):
                _ = self.commit_tip(ledger, {PARENT_FILE: contents})
                before = self.held_rows(ledger.database)
                left, = self.follow_approved(ledger, [finding_id])
                self.assertIsInstance(left, style_ledger.OutsideRenameUnresolved)
                self.assertEqual(self.held_rows(ledger.database), before)
                finding, = ledger.held_findings(style_ledger.AllHeldFindings())
                self.assertEqual(finding.state, style_ledger.Filed(self.root / "decision.md"))


    def test_an_unfiled_rename_follows_outside_code_while_its_approved_twin_is_built(self) -> None:
        ledger = self.ledger()
        _ = self.commit_tip(ledger, {PARENT_FILE: PARENT})
        _, (unfiled, filed) = self.hold_parent(ledger, ((1, "pub struct Parent {"), (2, "old: u32,")), before="Parent", after="Renamed")
        ledger.file_held(filed, self.root / "decision.md")
        _ = self.commit_tip(ledger, {PARENT_FILE: PARENT.replace("Parent", "Renamed")})
        outcomes = self.follow_approved(ledger, [unfiled, filed])
        self.assertEqual([type(outcome) for outcome in outcomes], [style_ledger.DeclarationMoved, style_ledger.DeclarationRenamed])
        states = {finding.finding_id: finding.state for finding in ledger.held_findings(style_ledger.AllHeldFindings())}
        self.assertEqual(states, {unfiled: style_ledger.Unfiled(), filed: style_ledger.Built(self.root / "decision.md")})


    def test_a_renamed_finding_that_an_open_build_ticket_holds_is_kept(self) -> None:
        ledger = self.ledger()
        finding_id = self.renamed_parent(ledger)
        ledger.ticket_add("names-1", "names", "proposal", "names", "crates/alpha", [PARENT_FILE], [self.rule("names")],
                          proposal=str(self.root / "decision.md"), finding_ids=(finding_id,))
        ledger.ticket_assign("names-1", "main")
        _ = self.commit_tip(ledger, {PARENT_FILE: PARENT.replace("Parent", "Renamed")})
        before = self.held_rows(ledger.database)
        kept, = self.follow_approved(ledger, [finding_id])
        self.assertEqual((type(kept), kept.finding.finding_id), (style_ledger.KeptForOpenBuild, finding_id))
        self.assertEqual(self.held_rows(ledger.database), before)
        # Once the ticket is final, the step builds the finding.
        ledger.ticket_drop("names-1")
        built, = self.follow_approved(ledger, [finding_id])
        self.assertEqual(type(built), style_ledger.DeclarationRenamed)


    def test_a_filed_rename_whose_after_name_its_file_already_declared_is_left(self) -> None:
        ledger = self.ledger()
        _ = self.commit_tip(ledger, {PARENT_FILE: PARENT + "\npub struct Renamed;\n"})
        _, (finding_id,) = self.judge_parent(ledger, ((1, "pub struct Parent {"),), before="Parent", after="Renamed")
        # Parent is deleted and Renamed stays where it was, so the rename was never applied.
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub struct Renamed;\n"})
        before = self.held_rows(ledger.database)
        left, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(left, style_ledger.OutsideRenameUnresolved)
        self.assertEqual(self.held_rows(ledger.database), before)
        self.assertIn("declaration not gained", repr(left))


    def test_a_filed_rename_whose_after_name_is_declared_with_another_keyword_is_left(self) -> None:
        ledger = self.ledger()
        finding_id = self.renamed_parent(ledger)
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub enum Renamed {\n    Old,\n}\n"})
        before = self.held_rows(ledger.database)
        left, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(left, style_ledger.OutsideRenameUnresolved)
        self.assertEqual(self.held_rows(ledger.database), before)
        self.assertIn("keyword differs", repr(left))


    def test_a_filed_rename_whose_after_name_one_file_in_another_crate_gained_is_left(self) -> None:
        ledger = self.ledger()
        finding_id = self.renamed_parent(ledger)
        other = "crates/beta/src/other.rs"
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub fn other() {}\n", other: PARENT.replace("Parent", "Renamed")})
        filed = ledger.held_findings(style_ledger.AllHeldFindings())[0]
        self.assert_rename_left(ledger, finding_id, style_ledger.DeclaredInAnotherCrate(
            filed, declarations.Declaration("struct", "Renamed", declarations.ModuleItem()), other))


    def test_a_filed_rename_whose_after_name_two_gaining_files_declare_is_left(self) -> None:
        ledger = self.ledger()
        finding_id = self.renamed_parent(ledger)
        renamed = PARENT.replace("Parent", "Renamed")
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub fn other() {}\n", MOVED_FILE: renamed, TWIN_FILE: renamed})
        filed = ledger.held_findings(style_ledger.AllHeldFindings())[0]
        self.assert_rename_left(ledger, finding_id, style_ledger.DeclaredInSeveralFiles(
            filed, declarations.Declaration("struct", "Renamed", declarations.ModuleItem()), (MOVED_FILE, TWIN_FILE)))


    def test_a_filed_rename_whose_site_still_declares_the_old_name_under_its_parent_is_left(self) -> None:
        ledger = self.ledger()
        method = "pub struct Parent {\n    old: u32,\n}\n\nimpl Parent {\n    pub fn old(&self) {}\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: method})
        # The applied check reads `r#new` as a name the file does not hold, and the field still declares Parent::old.
        _, (finding_id,) = self.judge_parent(ledger, ((6, "pub fn old(&self) {}"),), after="Parent::r#new")
        tip = self.commit_tip(ledger, {PARENT_FILE: method.replace("fn old", "fn r#new")})
        filed = ledger.held_findings(style_ledger.AllHeldFindings())[0]
        self.assertEqual(ledger.applied_check(filed, tip), RenameNotApplied(PARENT_FILE))
        self.assert_rename_left(ledger, finding_id, style_ledger.DeclarationGoneFromFile(
            filed, declarations.Declaration("fn", "old", declarations.ImplOrTraitItem("Parent"))))


    def test_a_filed_finding_whose_file_a_format_ignore_drops_from_the_rust_files_stays_filed(self) -> None:
        ledger = self.ledger()
        finding_id = self.renamed_parent(ledger)
        # The file leaves the synced Rust files but is still in the tree, so the finding waits for a person.
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub fn other() {}\n", "rustfmt.toml": f'ignore = ["{PARENT_FILE}"]\n'})
        filed = ledger.held_findings(style_ledger.AllHeldFindings())[0]
        self.assert_rename_left(ledger, finding_id, style_ledger.DeclarationGoneFromFile(
            filed, declarations.Declaration("struct", "Parent", declarations.ModuleItem())))


    def test_a_renamed_finding_whose_decision_loses_its_approval_before_the_lock_stays_filed(self) -> None:
        ledger = self.ledger()
        finding_id = self.renamed_parent(ledger)
        _ = self.commit_tip(ledger, {PARENT_FILE: PARENT.replace("Parent", "Renamed")})
        # The answer is approved at the plan, then not at the recheck under the write lock, nor after.
        answers = iter((True,))
        before = self.held_rows(ledger.database)
        waiting, = ledger.follow_declarations([finding_id], ledger.synced_commit(), lambda _: next(answers, False)).outcomes
        filed = ledger.held_findings(style_ledger.AllHeldFindings())[0]
        self.assertEqual(waiting, style_ledger.RenameAwaitingApproval(filed))
        self.assertEqual(self.held_rows(ledger.database), before)


    def test_the_declaration_step_refuses_when_the_synced_tip_moved_since_its_reading(self) -> None:
        ledger = self.ledger()
        finding_id = self.renamed_parent(ledger)
        read = ledger.synced_commit()
        _ = self.commit_tip(ledger, {PARENT_FILE: PARENT.replace("Parent", "Renamed")})
        before = self.held_rows(ledger.database)
        with self.assertRaisesRegex(style_ledger.LedgerError, f"synced tip moved from {read}"):
            _ = ledger.follow_declarations([finding_id], read, lambda _: True)
        self.assertEqual(self.held_rows(ledger.database), before)


    def test_the_declaration_step_refuses_when_the_synced_tip_moves_between_its_plan_and_the_write_lock(self) -> None:
        ledger = self.ledger()
        finding_id = self.renamed_parent(ledger)
        planned = self.commit_tip(ledger, {PARENT_FILE: PARENT.replace("Parent", "Renamed")})
        moved = False

        def read_during_a_sync(text: str) -> declarations.DeclaredFile:
            # A sync lands while the plan reads its first file.
            nonlocal moved
            if not moved:
                moved = True
                _ = self.commit_tip(ledger, {MOVED_FILE: "pub fn helper() {}\n"})
            return declarations.read_declarations(text)

        before = self.held_rows(ledger.database)
        with (mock.patch.object(style_ledger, "read_declarations", read_during_a_sync),
              self.assertRaisesRegex(style_ledger.LedgerError, f"synced tip moved from {planned}")):
            _ = ledger.follow_declarations([finding_id], planned, lambda _: True)
        self.assertEqual(self.held_rows(ledger.database), before)


    def test_a_built_rename_reads_its_file_at_each_commit_once_across_the_plan_and_its_recheck(self) -> None:
        ledger = self.ledger()
        finding_id = self.renamed_parent(ledger)
        _ = self.commit_tip(ledger, {PARENT_FILE: PARENT.replace("Parent", "Renamed")})
        with mock.patch.object(subprocess, "run", wraps=subprocess.run) as runs:
            renamed, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(renamed, style_ledger.DeclarationRenamed)
        reads = [call for call in runs.call_args_list
                 if PARENT_FILE in cast(tuple[list[str]], call.args)[0] and "ls-tree" in cast(tuple[list[str]], call.args)[0]]
        # a.rs at the read commit and at the tip; the applied check and the recheck under the lock read no git.
        self.assertEqual(len(reads), 2)


    def test_a_trait_qualified_built_rename_follows_a_filed_impl_method_and_keeps_its_type_owner(self) -> None:
        ledger = self.ledger()
        finding_id, source = self.trait_method_finding(ledger)
        _ = self.built_finding(ledger, PARENT_FILE, "Trait::old", "Trait::new", line=2)
        filed = next(row for row in ledger.held_findings(style_ledger.AllHeldFindings()) if row.finding_id == finding_id)
        tip = self.commit_tip(ledger, {PARENT_FILE: "// moved down\n" + source.replace("fn old", "fn new")})
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertEqual(outcome, style_ledger.DeclarationMoved(
            filed, declarations.Declaration("fn", "new", declarations.ImplOrTraitItem("Type")), PARENT_FILE, 8))
        with style_ledger.Ledger(self.repo, ledger.database) as reopened:
            followed = next(row for row in reopened.held_findings(style_ledger.AllHeldFindings()) if row.finding_id == finding_id)
            self.assertEqual((followed.finding_id, followed.path, followed.line, followed.state, followed.follow),
                             (finding_id, PARENT_FILE, 8, style_ledger.Filed(self.root / "decision.md"),
                              style_ledger.FollowedDeclaration(PARENT_FILE, 7, tip)))
            self.assertEqual((followed.before, followed.after), (filed.before, filed.after))


    def test_trait_qualified_renames_follow_two_impl_types_in_separate_files_and_leave_unfiled_findings_unfiled(self) -> None:
        ledger = self.ledger()
        source = "pub struct Type;\nimpl Trait for Type {\n    fn old(&self) -> u32 {\n        7\n    }\n}\n"
        other = source.replace("Type", "Other")
        _ = self.commit_tip(ledger, {PARENT_FILE: source, MOVED_FILE: other, TWIN_FILE: "pub trait Trait { fn old(&self) -> u32; }\n"})
        _, (filed,) = self.judge_parent(ledger, ((4, "7"),), kind="restructure")
        _, (unfiled,) = self.hold_parent(ledger, ((4, "7"),), kind="restructure", path=MOVED_FILE)
        _ = self.built_finding(ledger, TWIN_FILE, "Trait::old", "Trait::new")
        tip = self.commit_tip(ledger, {PARENT_FILE: source.replace("fn old", "fn new"),
                                       MOVED_FILE: other.replace("fn old", "fn new"),
                                       TWIN_FILE: "pub trait Trait { fn new(&self) -> u32; }\n"})
        outcomes = self.follow_approved(ledger, [filed, unfiled])
        self.assertEqual([type(outcome) for outcome in outcomes], [style_ledger.DeclarationMoved] * 2)
        rows = {row.finding_id: row for row in ledger.held_findings(style_ledger.AllHeldFindings())}
        for finding_id, path, state in ((filed, PARENT_FILE, style_ledger.Filed(self.root / "decision.md")),
                                        (unfiled, MOVED_FILE, style_ledger.Unfiled())):
            row = rows[finding_id]
            self.assertEqual((row.path, row.line, row.state, row.follow),
                             (path, 4, state, style_ledger.FollowedDeclaration(path, 4, tip)))
        before = self.held_rows(ledger.database)
        self.assertEqual([type(outcome) for outcome in self.follow_approved(ledger, [filed, unfiled])],
                         [style_ledger.DeclarationInPlace] * 2)
        self.assertEqual(self.held_rows(ledger.database), before)


    def test_inherent_source_keeps_trait_follow_identity(self) -> None:
        self.assert_trait_source_collision('impl Type')


    def test_other_trait_source_keeps_trait_follow_identity(self) -> None:
        self.assert_trait_source_collision('impl OtherTrait for Type')


    def test_inherent_destination_keeps_trait_follow_identity(self) -> None:
        self.assert_trait_destination_collision('impl Type')


    def test_other_trait_destination_keeps_trait_follow_identity(self) -> None:
        self.assert_trait_destination_collision('impl OtherTrait for Type')


    def test_trait_method_follow_composes_with_built_type_and_trait_renames(self) -> None:
        ledger = self.ledger()
        finding_id, source = self.trait_method_finding(ledger)
        _ = self.built_finding(ledger, PARENT_FILE, "Type", "RenamedType", line=4)
        _ = self.built_finding(ledger, PARENT_FILE, "Trait", "RenamedTrait", line=1)
        _ = self.built_finding(ledger, PARENT_FILE, "RenamedTrait::old", "RenamedTrait::new", line=2)
        _ = self.commit_tip(ledger, {PARENT_FILE: source.replace("Type", "RenamedType").replace("Trait", "RenamedTrait").replace("fn old", "fn new")})
        filed = next(row for row in ledger.held_findings(style_ledger.AllHeldFindings()) if row.finding_id == finding_id)
        moved, = self.follow_approved(ledger, [finding_id])
        self.assertEqual(moved, style_ledger.DeclarationMoved(
            filed, declarations.Declaration("fn", "new", declarations.ImplOrTraitItem("RenamedType")), PARENT_FILE, 7))


    def test_preexisting_destination_keeps_trait_follow_identity(self) -> None:
        self.assert_trait_destination_limit('preexisting')


    def test_two_destination_files_keeps_trait_follow_identity(self) -> None:
        self.assert_trait_destination_limit('two files')


    def test_two_destination_items_keeps_trait_follow_identity(self) -> None:
        self.assert_trait_destination_limit('two items')


    def test_unapproved_trait_rename_keeps_trait_follow_identity(self) -> None:
        self.assert_trait_follow_limit('unapproved')


    def test_trait_rename_from_another_crate_keeps_trait_follow_identity(self) -> None:
        self.assert_trait_follow_limit('other crate')


    def test_forked_trait_rename_keeps_trait_follow_identity(self) -> None:
        self.assert_trait_follow_limit('fork')


    def test_open_trait_build_ticket_keeps_trait_follow_identity(self) -> None:
        self.assert_trait_follow_limit('open build')


    def test_trait_rename_losing_approval_before_lock_keeps_trait_follow_identity(self) -> None:
        self.assert_trait_follow_limit('lost approval')


    def test_trait_method_rename_chains_stop_before_returning_to_the_original_name(self) -> None:
        ledger = self.ledger()
        finding_id, source = self.trait_method_finding(ledger)
        _ = self.built_finding(ledger, PARENT_FILE, "Trait::old", "Trait::middle", line=2)
        _ = self.built_finding(ledger, PARENT_FILE, "Trait::middle", "Trait::old", line=2)
        _ = self.commit_tip(ledger, {PARENT_FILE: source.replace("fn old", "fn middle")})
        filed = next(row for row in ledger.held_findings(style_ledger.AllHeldFindings()) if row.finding_id == finding_id)
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertEqual(outcome, style_ledger.DeclarationMoved(
            filed, declarations.Declaration("fn", "middle", declarations.ImplOrTraitItem("Type")), PARENT_FILE, 7))


    def test_an_open_judging_ticket_keeps_its_trait_method_finding_at_the_read_site(self) -> None:
        ledger = self.ledger()
        source = TRAIT_METHOD
        _ = self.commit_tip(ledger, {PARENT_FILE: source})
        rule = self.rule("names")
        ledger.ticket_add("judge-1", "judge", "read", "names", "crates/alpha", [PARENT_FILE], [rule])
        ledger.ticket_assign("judge-1", "main")
        _ = ledger.ticket_base("judge-1")
        finding = ledger.hold(rule, PARENT_FILE, 7, "7", "restructure", "body", "new body", self.blob(rule), "judge-1")
        _ = self.built_finding(ledger, PARENT_FILE, "Trait::old", "Trait::new", line=2)
        _ = self.commit_tip(ledger, {PARENT_FILE: source.replace("fn old", "fn new")})
        before = self.held_rows(ledger.database)
        outcome, = self.follow_approved(ledger, [finding.finding_id])
        self.assertIsInstance(outcome, style_ledger.DeclarationNotRead)
        self.assertEqual(self.held_rows(ledger.database), before)


    def test_a_readable_qualified_trait_path_follows_its_method_rename(self) -> None:
        ledger = self.ledger()
        source = "pub struct Type;\nimpl crate::traits::Trait for Type {\n    fn old(&self) -> u32 {\n        7\n    }\n}\n"
        source += "use crate::traits::Trait;\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: source})
        _, (finding_id,) = self.judge_parent(ledger, ((4, "7"),), kind="restructure")
        _ = self.built_finding(ledger, PARENT_FILE, "Trait::old", "Trait::new")
        _ = self.commit_tip(ledger, {PARENT_FILE: source.replace("fn old", "fn new")})
        filed = next(row for row in ledger.held_findings(style_ledger.AllHeldFindings()) if row.finding_id == finding_id)
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertEqual(outcome, style_ledger.DeclarationMoved(
            filed, declarations.Declaration("fn", "new", declarations.ImplOrTraitItem("Type")), PARENT_FILE, 4))


    def test_an_unreadable_implemented_trait_grants_no_method_follow(self) -> None:
        self.assert_changed_trait_membership_is_left("impl <Type as Family>::Trait for Type",
                                                     "impl <Type as Family>::Trait for Type")


    def test_matching_trait_leaf_names_do_not_hide_different_qualified_trait_paths(self) -> None:
        self.assert_changed_trait_membership_is_left("impl crate::first::Trait for Type",
                                                     "impl crate::second::Trait for Type")


    def test_an_aliased_trait_name_does_not_establish_a_trait_qualified_follow(self) -> None:
        ledger = self.ledger()
        source = "use crate::Trait as Alias;\npub struct Type;\nimpl Alias for Type {\n    fn old(&self) -> u32 {\n        7\n    }\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: source})
        _, (finding_id,) = self.judge_parent(ledger, ((5, "7"),), kind="restructure")
        _ = self.built_finding(ledger, PARENT_FILE, "Trait::old", "Trait::new")
        _ = self.commit_tip(ledger, {PARENT_FILE: source.replace("fn old", "fn new")})
        before = self.held_rows(ledger.database)
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertNotIsInstance(outcome, style_ledger.DeclarationMoved)
        self.assertEqual(self.held_rows(ledger.database), before)


    def test_an_inherent_self_rename_does_not_rename_a_traits_other_implementations(self) -> None:
        ledger = self.ledger()
        finding_id, source = self.trait_method_finding(ledger)
        inherent = "pub struct Other;\nimpl Other {\n    fn old() {}\n    fn caller() { Self::old(); }\n}\n"
        _ = self.commit_tip(ledger, {MOVED_FILE: inherent})
        _ = self.built_finding(ledger, MOVED_FILE, "Self::old", "Self::new", line=4)
        _ = self.commit_tip(ledger, {PARENT_FILE: source.replace("fn old", "fn new"), MOVED_FILE: inherent.replace("old", "new")})
        before = self.held_rows(ledger.database)
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertNotIsInstance(outcome, style_ledger.DeclarationMoved)
        self.assertEqual(self.held_rows(ledger.database), before)


    def test_qualifying_the_same_trait_keeps_an_unfiled_method_in_place(self) -> None:
        ledger = self.ledger()
        source = TRAIT_METHOD
        _ = self.commit_tip(ledger, {PARENT_FILE: source})
        _, (finding_id,) = self.hold_parent(ledger, ((7, "7"),), kind="restructure")
        _ = self.commit_tip(ledger, {PARENT_FILE: source.replace("impl Trait", "impl crate::a::Trait")})
        before = self.held_rows(ledger.database)
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(outcome, style_ledger.DeclarationInPlace)
        self.assertEqual(self.held_rows(ledger.database), before)


    def test_an_import_proves_trait_identity_when_other_modules_declare_the_same_name(self) -> None:
        ledger = self.ledger()
        source = "use crate::b::Trait;\npub struct Type;\nimpl Trait for Type {\n    fn old(&self) { 7 }\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: source, MOVED_FILE: "pub trait Trait {}\n", TWIN_FILE: "pub trait Trait {}\n"})
        _, (finding_id,) = self.hold_parent(ledger, ((4, "7"),), kind="restructure")
        _ = self.commit_tip(ledger, {PARENT_FILE: source.replace("impl Trait", "impl crate::b::Trait")})
        before = self.held_rows(ledger.database)
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(outcome, style_ledger.DeclarationInPlace)
        self.assertEqual(self.held_rows(ledger.database), before)


    def test_an_uncertain_trait_qualification_keeps_the_unfiled_row_unchanged(self) -> None:
        ledger = self.ledger()
        source = "impl Trait for Type {\n    fn old(&self) { 7 }\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: source})
        _, (finding_id,) = self.hold_parent(ledger, ((2, "7"),), kind="restructure")
        _ = self.commit_tip(ledger, {PARENT_FILE: source.replace("impl Trait", "impl external::Trait")})
        before = self.held_rows(ledger.database)
        planned, = ledger.declaration_reading([finding_id], lambda _: True).outcomes
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertEqual(type(outcome).__name__, "TraitIdentityUncertain")
        self.assertEqual(outcome, planned)
        self.assertEqual(self.held_rows(ledger.database), before)


    def test_an_unresolved_built_trait_qualifier_cannot_rename_another_modules_impl(self) -> None:
        ledger = self.ledger()
        source = "impl crate::b::Trait for Type {\n    fn old(&self) { 7 }\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: source, MOVED_FILE: "pub trait Trait { fn old() {} fn new() {} }\n",
                                       TWIN_FILE: "pub trait Trait { fn old() {} fn new() {} }\n"})
        _, (finding_id,) = self.hold_parent(ledger, ((2, "7"),), kind="restructure")
        _ = self.built_finding(ledger, PARENT_FILE, "Trait::old", "Trait::new")
        _ = self.commit_tip(ledger, {PARENT_FILE: source.replace("fn old", "fn new")})
        before = self.held_rows(ledger.database)
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertEqual(type(outcome).__name__, "TraitIdentityUncertain")
        self.assertEqual(self.held_rows(ledger.database), before)


    def test_a_module_trait_rename_cannot_change_a_different_modules_trait(self) -> None:
        ledger = self.ledger()
        source = "impl crate::b::Trait for Type {\n    fn old(&self) { 7 }\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: source, MOVED_FILE: "pub trait Trait {}\n", TWIN_FILE: "pub trait Trait {}\n"})
        _, (finding_id,) = self.judge_parent(ledger, ((2, "7"),), kind="restructure")
        _ = self.built_finding(ledger, TWIN_FILE, "c::Trait", "c::NewTrait")
        _ = self.built_finding(ledger, TWIN_FILE, "NewTrait::old", "NewTrait::new")
        _ = self.commit_tip(ledger, {PARENT_FILE: source.replace("b::Trait", "b::NewTrait").replace("fn old", "fn new"),
                                       TWIN_FILE: "pub trait NewTrait {}\n"})
        before = self.held_rows(ledger.database)
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertNotIsInstance(outcome, style_ledger.DeclarationMoved)
        self.assertEqual(self.held_rows(ledger.database), before)


    def test_a_method_rename_does_not_follow_between_different_trait_arguments(self) -> None:
        ledger = self.ledger()
        source = "pub trait Trait<T> { fn old() {} fn new() {} }\nimpl Trait<u8> for Type {\n    fn old(&self) { 7 }\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: source})
        _, (finding_id,) = self.judge_parent(ledger, ((3, "7"),), kind="restructure")
        _ = self.built_finding(ledger, PARENT_FILE, "Trait::old", "Trait::new")
        _ = self.commit_tip(ledger, {PARENT_FILE: source.replace("Trait<u8>", "Trait<u16>").replace("fn old(&self)", "fn new(&self)")})
        before = self.held_rows(ledger.database)
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertNotIsInstance(outcome, style_ledger.DeclarationMoved)
        self.assertEqual(self.held_rows(ledger.database), before)


    def test_a_trait_method_follows_to_a_proved_equal_qualified_destination(self) -> None:
        ledger = self.ledger()
        source = "impl Trait for Type {\n    fn old(&self) { 7 }\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: source, TWIN_FILE: "pub trait Trait { fn old() {} }\n"})
        _, (finding_id,) = self.hold_parent(ledger, ((2, "7"),), kind="restructure")
        _ = self.built_finding(ledger, TWIN_FILE, "Trait::old", "Trait::new")
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub fn helper() {}\n",
                                       MOVED_FILE: source.replace("impl Trait", "impl crate::c::Trait").replace("fn old", "fn new"),
                                       TWIN_FILE: "pub trait Trait { fn new() {} }\n"})
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(outcome, style_ledger.DeclarationMoved)
        row = next(row for row in ledger.held_findings(style_ledger.AllHeldFindings()) if row.finding_id == finding_id)
        self.assertEqual((row.path, row.line, row.state), (MOVED_FILE, 2, style_ledger.Unfiled()))


    def test_unreadable_trait_arguments_keep_the_finding_instead_of_withdrawing_it(self) -> None:
        ledger = self.ledger()
        source = "impl Trait<u8>::Assoc for Type {\n    fn old(&self) { 7 }\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: source})
        _, (finding_id,) = self.hold_parent(ledger, ((2, "7"),), kind="restructure")
        _ = self.built_finding(ledger, PARENT_FILE, "Trait::old", "Trait::new")
        _ = self.commit_tip(ledger, {PARENT_FILE: source.replace("fn old", "fn new")})
        before = self.held_rows(ledger.database)
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertEqual(type(outcome).__name__, "TraitIdentityUncertain")
        self.assertEqual(self.held_rows(ledger.database), before)


    def test_a_built_method_rename_in_another_traits_module_grants_no_follow(self) -> None:
        ledger = self.ledger()
        source = "impl crate::b::Trait for Type {\n    fn old(&self) { 7 }\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: source,
                                       MOVED_FILE: "pub trait Trait { fn old() {} fn new() {} }\n",
                                       TWIN_FILE: "pub trait Trait { fn old() {} fn new() {} }\n"})
        _, (finding_id,) = self.judge_parent(ledger, ((2, "7"),), kind="restructure")
        _ = self.built_finding(ledger, TWIN_FILE, "Trait::old", "Trait::new")
        _ = self.commit_tip(ledger, {PARENT_FILE: source.replace("fn old", "fn new")})
        before = self.held_rows(ledger.database)
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertNotIsInstance(outcome, style_ledger.DeclarationMoved)
        self.assertEqual(self.held_rows(ledger.database), before)


    def test_a_plain_trait_alias_keeps_the_same_qualified_impl_in_place(self) -> None:
        ledger = self.ledger()
        source = "use crate::b::Trait as Alias;\nimpl Alias for Type {\n    fn old(&self) { 7 }\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: source, MOVED_FILE: "pub trait Trait {}\n"})
        _, (finding_id,) = self.hold_parent(ledger, ((3, "7"),), kind="restructure")
        _ = self.commit_tip(ledger, {PARENT_FILE: source.replace("impl Alias", "impl crate::b::Trait")})
        before = self.held_rows(ledger.database)
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(outcome, style_ledger.DeclarationInPlace)
        self.assertEqual(self.held_rows(ledger.database), before)


    def test_an_expanded_trait_alias_does_not_match_another_nested_modules_reexport(self) -> None:
        ledger = self.ledger()
        source = "use crate::one::nested::Trait as Alias;\nimpl Alias for Type {\n    fn old(&self) { 7 }\n}\n"
        _ = self.commit_tip(ledger, {
            PARENT_FILE: source,
            "crates/alpha/src/lib.rs": "mod a; mod b; mod other; mod one; mod two;\n",
            "crates/alpha/src/one.rs": "mod nested;\n",
            "crates/alpha/src/two.rs": "mod nested;\n",
            "crates/alpha/src/one/nested.rs": "pub trait Trait { fn old() {} fn new() {} }\n",
            "crates/alpha/src/two/nested.rs": "pub use crate::other::Other as Trait;\n",
            "crates/alpha/src/other.rs": "pub trait Other { fn old() {} fn new() {} }\n",
        })
        _, (finding_id,) = self.judge_parent(ledger, ((3, "7"),), kind="restructure")
        _ = self.built_finding(ledger, PARENT_FILE, "Alias::old", "Alias::new", line=3)
        _ = self.commit_tip(ledger, {
            PARENT_FILE: "use crate::one::nested::Trait as Alias;\n",
            MOVED_FILE: "impl crate::two::nested::Trait for Type {\n    fn new(&self) { 7 }\n}\n",
        })
        before = self.held_rows(ledger.database)
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(outcome, style_ledger.TraitIdentityUncertain)
        self.assertEqual(self.held_rows(ledger.database), before)
        _ = self.commit_tip(ledger, {
            MOVED_FILE: None,
            "crates/alpha/src/one/nested.rs": (
                "pub trait Trait { fn old() {} fn new() {} }\n"
                "impl Trait for Type {\n    fn new(&self) { 7 }\n}\n"
            ),
        })
        proved, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(proved, style_ledger.DeclarationMoved)
        self.assertEqual(cast(style_ledger.DeclarationMoved, proved).path, "crates/alpha/src/one/nested.rs")


    def test_growing_trait_alias_cycles_stop_without_following_or_withdrawing(self) -> None:
        ledger = self.ledger()
        source = "use b::a;\nuse a::b;\nimpl a::Trait for Type {\n    fn old(&self) { 7 }\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: source})
        _, (finding_id,) = self.hold_parent(ledger, ((4, "7"),), kind="restructure")
        _ = self.commit_tip(ledger, {PARENT_FILE: source.replace("impl a::Trait", "impl crate::b::Trait")})
        before = self.held_rows(ledger.database)
        # Guard against nontermination if alias expansion keeps growing its path.
        program = (
            "from pathlib import Path; import style_ledger; import sys; "
            "ledger = style_ledger.Ledger(Path(sys.argv[1]), Path(sys.argv[2])); "
            "outcome, = ledger.follow_declarations([sys.argv[3]], ledger.synced_commit(), approved=lambda path: True).outcomes; "
            "print(type(outcome).__name__); ledger.close()"
        )
        result = subprocess.run([sys.executable, "-c", program, str(self.repo), str(ledger.database), finding_id],
                                capture_output=True, text=True, check=True, timeout=TEST_HANG_GUARD_SECS,
                                env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)})
        self.assertEqual(result.stdout.strip(), "TraitIdentityUncertain")
        self.assertEqual(self.held_rows(ledger.database), before)


    def test_unsupported_alias_sources_keep_trait_identity_uncertain(self) -> None:
        for index, (prefix, trait) in enumerate((
            ("#[cfg(test)] use crate::b::Trait as Alias;\n", "Alias"),
            ("mod imports { use crate::b::Trait as Alias;\n", "Alias"),
            ("pub use crate::b::Trait as Alias;\n", "Alias"),
            ("use external::*;\n", "Alias"),
            ("mod nested {\n", "self::Alias"),
            ("mod nested {\n", "super::Alias"),
        )):
            with self.subTest(prefix=prefix, trait=trait):
                ledger = style_ledger.Ledger(self.repo, self.root / f"aliases-{index}.db")
                self.addCleanup(ledger.close)
                source = prefix + f"impl {trait} for Type {{\n    fn old(&self) {{ 7 }}\n}}\n"
                if prefix.startswith(("mod imports", "mod nested")):
                    source += "}\n"
                _ = self.commit_tip(ledger, {PARENT_FILE: source, MOVED_FILE: "pub trait Trait {}\n"})
                _, (finding_id,) = self.hold_parent(ledger, ((len(prefix.splitlines()) + 2, "7"),),
                                                     kind="restructure", before=prefix + trait)
                _ = self.commit_tip(ledger, {PARENT_FILE: source.replace(f"impl {trait}", "impl crate::b::Trait")})
                before = self.held_rows(ledger.database)
                outcome, = self.follow_approved(ledger, [finding_id])
                self.assertIsInstance(outcome, style_ledger.TraitIdentityUncertain)
                self.assertEqual(self.held_rows(ledger.database), before)


    def test_a_built_trait_alias_cannot_authorize_a_different_traits_method(self) -> None:
        ledger = self.ledger()
        source = "impl crate::b::Trait for Type {\n    fn old(&self) { 7 }\n}\n"
        built_site = "use crate::other::Other as Trait;\npub fn caller() { Trait::old(); }\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: source, MOVED_FILE: "pub trait Trait {}\n", TWIN_FILE: built_site,
                                   "crates/alpha/src/other.rs": "pub trait Other {}\n"})
        _, (finding_id,) = self.judge_parent(ledger, ((2, "7"),), kind="restructure")
        _ = self.built_finding(ledger, TWIN_FILE, "Trait::old", "Trait::new", line=2)
        _ = self.commit_tip(ledger, {PARENT_FILE: source.replace("fn old", "fn new")})
        before = self.held_rows(ledger.database)
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(outcome, style_ledger.DeclarationGoneFromFile)
        self.assertEqual(self.held_rows(ledger.database), before)


    def test_unsupported_imports_block_built_trait_declaration_fallback(self) -> None:
        for index, imported in enumerate(("#[cfg(test)] use crate::other::Other as Trait;",
                         "mod imports { use crate::other::Other as Trait;",
                         "use external::*;", "pub use crate::other::Other as Trait;")):
            with self.subTest(imported=imported):
                ledger = style_ledger.Ledger(self.repo, self.root / f"blocked-{index}.db")
                self.addCleanup(ledger.close)
                source = "impl crate::b::Trait for Type {\n    fn old(&self) { 7 }\n}\n"
                built_site = imported + "\npub fn caller() { Trait::old(); }\n"
                if imported.startswith("mod imports"):
                    built_site += "}\n"
                _ = self.commit_tip(ledger, {PARENT_FILE: source, MOVED_FILE: "pub trait Trait {}\n",
                                           TWIN_FILE: built_site})
                _, (finding_id,) = self.judge_parent(ledger, ((2, "7"),), kind="restructure", before=imported)
                _ = self.built_finding(ledger, TWIN_FILE, "Trait::old", "Trait::new", line=2)
                _ = self.commit_tip(ledger, {PARENT_FILE: source.replace("fn old", "fn new")})
                before = self.held_rows(ledger.database)
                outcome, = self.follow_approved(ledger, [finding_id])
                self.assertIsInstance(outcome, style_ledger.TraitIdentityUncertain)
                self.assertEqual(self.held_rows(ledger.database), before)


    def test_a_real_built_trait_rename_follows_its_dependent_impl_method(self) -> None:
        ledger = self.ledger()
        source = "pub trait Trait {\n    fn old() {}\n}\nimpl Trait for Type {\n    fn old(&self) { 7 }\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: source})
        _, (method,) = self.judge_parent(ledger, ((5, "7"),), kind="restructure")
        _, (rename,) = self.judge_parent(ledger, ((1, "pub trait Trait"),), before="Trait", after="RenamedTrait")
        _ = self.commit_tip(ledger, {PARENT_FILE: source.replace("Trait", "RenamedTrait")})
        renamed, = self.follow_approved(ledger, [rename])
        self.assertIsInstance(renamed, style_ledger.DeclarationRenamed)
        followed, = self.follow_approved(ledger, [method])
        self.assertIsInstance(followed, style_ledger.DeclarationMoved)
        self.assertEqual(cast(style_ledger.DeclarationMoved, followed).declaration,
                         declarations.Declaration("fn", "old", declarations.ImplOrTraitItem("Type")))
        again, = self.follow_approved(ledger, [method])
        self.assertIsInstance(again, style_ledger.DeclarationInPlace)


    def test_an_uncertain_built_rename_without_a_gained_destination_does_not_keep_a_deleted_method(self) -> None:
        ledger = self.ledger()
        source = "impl crate::b::Trait for Type {\n    fn old(&self) { 7 }\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: source, MOVED_FILE: "pub trait Trait {}\n", TWIN_FILE: "pub trait Trait {}\n"})
        _, (finding_id,) = self.hold_parent(ledger, ((2, "7"),), kind="restructure")
        _ = self.built_finding(ledger, PARENT_FILE, "Trait::old", "Trait::new")
        _ = self.commit_tip(ledger, {PARENT_FILE: "impl crate::b::Trait for Type {}\n"})
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(outcome, style_ledger.DeclarationRemoved)
        row = next(row for row in ledger.held_findings(style_ledger.AllHeldFindings()) if row.finding_id == finding_id)
        self.assertIsInstance(row.state, style_ledger.Withdrawn)


    def test_an_unrelated_built_name_in_another_crate_does_not_withdraw_an_outside_rename(self) -> None:
        ledger = self.ledger()
        source = "fn original() {\n    first();\n    second();\n}\n"
        other = "crates/beta/src/other.rs"
        _ = self.commit_tip(ledger, {PARENT_FILE: source, other: source.replace("original", "unrelated")})
        _, (finding_id,) = self.hold_parent(ledger, ((1, "fn original() {"),), kind="restructure")
        _ = self.built_finding(ledger, other, "original", "elsewhere")
        _ = self.commit_tip(ledger, {PARENT_FILE: source.replace("original", "renamed")})
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(outcome, style_ledger.DeclarationMoved)
        row = next(row for row in ledger.held_findings(style_ledger.AllHeldFindings()) if row.finding_id == finding_id)
        self.assertEqual((row.path, row.state), (PARENT_FILE, style_ledger.Unfiled()))
        self.assertIsInstance(row.follow, style_ledger.FollowedDeclaration)


    def test_a_built_rename_in_another_module_does_not_withdraw_an_outside_rename(self) -> None:
        ledger = self.ledger()
        source = "fn original() {\n    first();\n    second();\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: source, MOVED_FILE: "fn elsewhere() {}\n"})
        _, (finding_id,) = self.hold_parent(ledger, ((1, "fn original() {"),), kind="restructure")
        _ = self.built_finding(ledger, MOVED_FILE, "b::original", "b::elsewhere")
        _ = self.commit_tip(ledger, {PARENT_FILE: source.replace("original", "renamed")})
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(outcome, style_ledger.DeclarationMoved)
        row = next(row for row in ledger.held_findings(style_ledger.AllHeldFindings()) if row.finding_id == finding_id)
        self.assertEqual((row.path, row.state), (PARENT_FILE, style_ledger.Unfiled()))
        self.assertIsInstance(row.follow, style_ledger.FollowedDeclaration)


    def test_outside_method_renames_require_the_same_resolved_generic_argument(self) -> None:
        for changed in (False, True):
            with self.subTest(argument_changed=changed):
                with style_ledger.Ledger(self.repo, self.root / f"generic-owner-{changed}.db") as ledger:
                    source = ("use crate::a::one::Arg as A;\nmod one { pub struct Arg; }\n"
                              + "mod two { pub struct Arg; }\nstruct Holder<T>(T);\n"
                              + "impl crate::a::Holder<A> {\n    fn original(&self) -> u32 {\n        1\n    }\n}\n")
                    _ = self.commit_tip(ledger, {PARENT_FILE: source})
                    _, (finding_id,) = self.judge_parent(ledger, ((6, "fn original(&self) -> u32 {"),), kind="restructure")
                    target = source.replace("fn original", "fn renamed")
                    if changed:
                        target = target.replace("use crate::a::one::Arg", "use crate::a::two::Arg")
                    _ = self.commit_tip(ledger, {PARENT_FILE: target})
                    before = self.held_rows(ledger.database)
                    outcome, = self.follow_approved(ledger, [finding_id])
                    if changed:
                        self.assertIsInstance(outcome, style_ledger.OutsideRenameUnresolved)
                        self.assertIn(style_ledger.OutsideFailure.WRONG_OWNER, repr(outcome))
                        self.assertEqual(self.held_rows(ledger.database), before)
                    else:
                        self.assertIsInstance(outcome, style_ledger.DeclarationMoved)
                        row = next(row for row in ledger.held_findings(style_ledger.AllHeldFindings()) if row.finding_id == finding_id)
                        self.assertEqual((row.path, row.state), (PARENT_FILE, style_ledger.Filed(self.root / "decision.md")))


    def test_outside_method_renames_distinguish_external_arguments_from_local_paths(self) -> None:
        for argument, local in (("crate::a::std::string::String", True),
                                ("std::string::String", True), ("std::string::String", False)):
            with self.subTest(argument=argument, local_root_added=local):
                with style_ledger.Ledger(self.repo, self.root / f"external-owner-{local}-{argument.count('::')}.db") as ledger:
                    source = ("struct Holder<T>(T);\nimpl Holder<std::string::String> {\n"
                              + "    fn original(&self) -> u32 {\n        1\n    }\n}\n")
                    _ = self.commit_tip(ledger, {PARENT_FILE: source})
                    _, (finding_id,) = self.judge_parent(ledger, ((3, "fn original(&self) -> u32 {"),), kind="restructure")
                    target = source.replace("fn original", "fn renamed").replace("std::string::String", argument)
                    if local:
                        target += "mod std { pub mod string { pub struct String; } }\n"
                    _ = self.commit_tip(ledger, {PARENT_FILE: target})
                    before = self.held_rows(ledger.database)
                    outcome, = self.follow_approved(ledger, [finding_id])
                    if local:
                        self.assertIsInstance(outcome, style_ledger.OutsideRenameUnresolved)
                        self.assertIn(style_ledger.OutsideFailure.WRONG_OWNER, repr(outcome))
                        self.assertEqual(self.held_rows(ledger.database), before)
                    else:
                        self.assertIsInstance(outcome, style_ledger.DeclarationMoved)
                        row = next(row for row in ledger.held_findings(style_ledger.AllHeldFindings()) if row.finding_id == finding_id)
                        self.assertEqual((row.path, row.state), (PARENT_FILE, style_ledger.Filed(self.root / "decision.md")))
                        self.assertIsInstance(row.follow, style_ledger.FollowedDeclaration)


    def test_outside_method_renames_require_the_same_external_crate_binding(self) -> None:
        for binding in ("std", "beta"):
            with self.subTest(destination_crate=binding):
                with style_ledger.Ledger(self.repo, self.root / f"external-binding-{binding}.db") as ledger:
                    source = ("extern crate std as external;\npub struct Holder<T>(T);\n"
                              + "impl crate::a::Holder<external::string::String> {\n"
                              + "    fn original(&self) -> u32 {\n        7\n    }\n}\n")
                    target = (f"extern crate {binding} as external;\n"
                              + "impl crate::a::Holder<external::string::String> {\n"
                              + "    fn renamed(&self) -> u32 {\n        7\n    }\n}\n")
                    _ = self.commit_tip(ledger, {PARENT_FILE: source, MOVED_FILE: "// empty\n",
                                                 "crates/beta/src/lib.rs": "pub mod string { pub struct String; }\n"})
                    _, (finding_id,) = self.judge_parent(ledger, ((4, "fn original(&self) -> u32 {"),), kind="restructure")
                    _ = self.commit_tip(ledger, {PARENT_FILE: "pub struct Holder<T>(T);\n", MOVED_FILE: target})
                    before = self.held_rows(ledger.database)
                    outcome, = self.follow_approved(ledger, [finding_id])
                    self.assertIsInstance(outcome, style_ledger.OutsideRenameUnresolved)
                    self.assertIn(style_ledger.OutsideFailure.WRONG_OWNER, repr(outcome))
                    self.assertEqual(self.held_rows(ledger.database), before)


    def test_crate_root_extern_aliases_do_not_prove_an_outside_method_rename(self) -> None:
        ledger = self.ledger()
        source = ("extern crate beta as external;\npub struct Holder<T>(T);\n"
                  + "impl crate::a::Holder<external::string::String> {\n"
                  + "    fn original(&self) -> u32 {\n        7\n    }\n}\n")
        target = ("impl crate::a::Holder<beta::string::String> {\n"
                  + "    fn renamed(&self) -> u32 {\n        7\n    }\n}\n")
        _ = self.commit_tip(ledger, {
            PARENT_FILE: source, MOVED_FILE: "// empty\n",
            "crates/alpha/src/lib.rs": "extern crate std as beta;\nmod a; mod b; mod c; mod other;\n",
            "crates/alpha/Cargo.toml": '[package]\nname="alpha"\nversion="0.1.0"\nedition="2021"\n'
                + '[dependencies]\nbeta={path="../beta"}\n',
            "crates/beta/src/lib.rs": "pub mod string { pub struct String; }\n",
        })
        _, (finding_id,) = self.judge_parent(ledger, ((4, "fn original(&self) -> u32 {"),), kind="restructure")
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub struct Holder<T>(T);\n", MOVED_FILE: target})
        before = self.held_rows(ledger.database)
        outcome, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(outcome, style_ledger.OutsideRenameUnresolved)
        self.assertIn(style_ledger.OutsideFailure.WRONG_OWNER, repr(outcome))
        self.assertEqual(self.held_rows(ledger.database), before)


    def test_extern_bindings_in_either_revision_keep_outside_method_findings_unchanged(self) -> None:
        root = "crates/alpha/src/lib.rs"
        for file in (PARENT_FILE, MOVED_FILE, root):
            for revision in ("before", "after"):
                with self.subTest(file=file, revision=revision):
                    with style_ledger.Ledger(self.repo, self.root / f"extern-{Path(file).stem}-{revision}.db") as ledger:
                        source = ("pub struct Holder<T>(T);\nimpl crate::a::Holder<std::string::String> {\n"
                                  + "    fn original(&self) -> u32 {\n        7\n    }\n}\n")
                        target = ("impl crate::a::Holder<std::string::String> {\n"
                                  + "    fn renamed(&self) -> u32 {\n        7\n    }\n}\n")
                        before_files: dict[str, str | None] = {
                            PARENT_FILE: source, MOVED_FILE: "// empty\n", root: "mod a; mod b; mod c; mod other;\n",
                        }
                        after_files: dict[str, str | None] = {
                            PARENT_FILE: "pub struct Holder<T>(T);\n", MOVED_FILE: target,
                            root: "mod a; mod b; mod c; mod other;\n",
                        }
                        files = before_files if revision == "before" else after_files
                        files[file] = "extern crate std as external;\n" + str(files[file])
                        _ = self.commit_tip(ledger, before_files)
                        line = 4 if file == PARENT_FILE and revision == "before" else 3
                        _, (finding_id,) = self.judge_parent(ledger, ((line, "fn original(&self) -> u32 {"),), kind="restructure")
                        _ = self.commit_tip(ledger, after_files)
                        before = self.held_rows(ledger.database)
                        outcome, = self.follow_approved(ledger, [finding_id])
                        self.assertIsInstance(outcome, style_ledger.OutsideRenameUnresolved)
                        self.assertIn(style_ledger.OutsideFailure.WRONG_OWNER, repr(outcome))
                        self.assertEqual(self.held_rows(ledger.database), before)


    def test_a_finding_whose_item_a_built_rename_renamed_follows_the_new_name_in_its_file(self) -> None:
        ledger = self.ledger()
        finding_id = self.restructured_parent(ledger)
        _ = self.built_finding(ledger, PARENT_FILE, "Parent", "Renamed")
        tip = self.commit_tip(ledger, {PARENT_FILE: PARENT.replace("Parent", "Renamed")})
        filed = ledger.held_findings(style_ledger.HeldFindingsInState("filed"))[0]
        moved, = self.follow_approved(ledger, [finding_id])
        self.assertEqual(moved, style_ledger.DeclarationMoved(
            filed, declarations.Declaration("struct", "Renamed", declarations.ModuleItem()), PARENT_FILE, 2))
        followed, = ledger.held_findings(style_ledger.HeldFindingsInState("filed"))
        self.assertEqual((followed.finding_id, followed.path, followed.line, followed.state, followed.follow),
                         (finding_id, PARENT_FILE, 2, style_ledger.Filed(self.root / "decision.md"),
                          style_ledger.FollowedDeclaration(PARENT_FILE, 2, tip)))
        # A second run reads the finding at its follow commit.
        before = self.held_rows(ledger.database)
        in_place, = self.follow_approved(ledger, [finding_id])
        self.assertEqual(in_place, style_ledger.DeclarationInPlace(followed))
        self.assertEqual(self.held_rows(ledger.database), before)


    def test_a_method_whose_impl_type_a_built_type_finding_renamed_follows_the_renamed_owner(self) -> None:
        ledger = self.ledger()
        method = "pub enum Parent {\n    A,\n}\n\nimpl Parent {\n    pub fn consume(&self) {}\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: method})
        _, (finding_id,) = self.judge_parent(ledger, ((6, "pub fn consume(&self) {}"),), kind="type")
        _ = self.built_finding(ledger, PARENT_FILE, "enum Parent { A }", "enum Renamed { A }", kind="type")
        _ = self.commit_tip(ledger, {PARENT_FILE: method.replace("Parent", "Renamed")})
        filed = ledger.held_findings(style_ledger.HeldFindingsInState("filed"))[0]
        moved, = self.follow_approved(ledger, [finding_id])
        self.assertEqual(moved, style_ledger.DeclarationMoved(
            filed, declarations.Declaration("fn", "consume", declarations.ImplOrTraitItem("Renamed")), PARENT_FILE, 6))


    def test_a_binding_rename_inside_a_renamed_fn_follows_the_fn_and_stays_filed(self) -> None:
        ledger = self.ledger()
        finding_id = self.renamed_binding(ledger, PARENT_FILE, "old", "new")
        followed, = ledger.held_findings(style_ledger.HeldFindingsInState("filed"))
        self.assertEqual((followed.finding_id, followed.state), (finding_id, style_ledger.Filed(self.root / "decision.md")))


    def test_a_built_rename_at_a_call_site_in_the_same_crate_moves_the_finding(self) -> None:
        ledger = self.ledger()
        _ = self.renamed_binding(ledger, MOVED_FILE, "a::old", "a::new")


    def test_a_built_rename_whose_after_adds_a_note_moves_the_finding_to_the_head_name(self) -> None:
        ledger = self.ledger()
        _ = self.renamed_binding(ledger, PARENT_FILE, "old", "new; rename the connected satellites")


    def test_an_unfiled_finding_whose_item_a_built_rename_renamed_follows_and_is_not_withdrawn(self) -> None:
        ledger = self.ledger()
        _ = self.commit_tip(ledger, {PARENT_FILE: PARENT})
        _, (finding_id,) = self.hold_parent(ledger, ((2, "old: u32,"),), kind="restructure")
        _ = self.built_finding(ledger, PARENT_FILE, "Parent", "Renamed")
        _ = self.commit_tip(ledger, {PARENT_FILE: PARENT.replace("Parent", "Renamed")})
        unfiled = ledger.held_findings(style_ledger.HeldFindingsInState("unfiled"))[0]
        moved, = self.follow_approved(ledger, [finding_id])
        self.assertEqual(moved, style_ledger.DeclarationMoved(
            unfiled, declarations.Declaration("struct", "Renamed", declarations.ModuleItem()), PARENT_FILE, 2))
        followed, = ledger.held_findings(style_ledger.HeldFindingsInState("unfiled"))
        self.assertEqual((followed.finding_id, followed.state), (finding_id, style_ledger.Unfiled()))


    def test_a_finding_whose_renamed_item_two_files_gained_is_left(self) -> None:
        ledger = self.ledger()
        finding_id = self.restructured_parent(ledger)
        _ = self.built_finding(ledger, PARENT_FILE, "Parent", "Renamed")
        renamed = PARENT.replace("Parent", "Renamed")
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub fn other() {}\n", MOVED_FILE: renamed, TWIN_FILE: renamed})
        filed = ledger.held_findings(style_ledger.HeldFindingsInState("filed"))[0]
        self.assert_rename_left(ledger, finding_id, style_ledger.DeclaredInSeveralFiles(
            filed, declarations.Declaration("struct", "Renamed", declarations.ModuleItem()), (MOVED_FILE, TWIN_FILE)))


    def test_a_finding_whose_item_a_rename_of_an_unapproved_decision_renamed_is_left(self) -> None:
        ledger = self.ledger()
        finding_id = self.restructured_parent(ledger)
        _ = self.built_finding(ledger, PARENT_FILE, "Parent", "Renamed")
        _ = self.commit_tip(ledger, {PARENT_FILE: PARENT.replace("Parent", "Renamed")})
        self.assert_gone_from_file(ledger, finding_id, lambda path: path != self.root / "built.md")


    def test_a_finding_whose_item_a_built_rename_in_another_crate_renamed_is_left(self) -> None:
        ledger = self.ledger()
        finding_id = self.restructured_parent(ledger)
        _ = self.built_finding(ledger, "crates/beta/src/other.rs", "Parent", "Renamed")
        _ = self.commit_tip(ledger, {PARENT_FILE: PARENT.replace("Parent", "Renamed")})
        self.assert_gone_from_file(ledger, finding_id, lambda _: True)


    def test_a_finding_whose_item_two_built_renames_name_differently_is_left(self) -> None:
        ledger = self.ledger()
        finding_id = self.restructured_parent(ledger)
        _ = self.built_finding(ledger, PARENT_FILE, "Parent", "Renamed")
        _ = self.built_finding(ledger, PARENT_FILE, "Parent", "Other")
        _ = self.commit_tip(ledger, {PARENT_FILE: PARENT.replace("Parent", "Renamed")})
        self.assert_gone_from_file(ledger, finding_id, lambda _: True)


    def test_an_unfiled_rename_whose_own_rename_a_built_twin_did_is_still_withdrawn(self) -> None:
        ledger = self.ledger()
        _ = self.commit_tip(ledger, {PARENT_FILE: PARENT})
        _, (finding_id,) = self.hold_parent(ledger, ((1, "pub struct Parent {"),), before="Parent", after="Renamed")
        _ = self.built_finding(ledger, PARENT_FILE, "Parent", "Renamed")
        tip = self.commit_tip(ledger, {PARENT_FILE: PARENT.replace("Parent", "Renamed")})
        removed, = self.follow_approved(ledger, [finding_id])
        self.assertEqual((type(removed), removed.finding.finding_id), (style_ledger.DeclarationRemoved, finding_id))
        withdrawn = next(finding for finding in ledger.held_findings(style_ledger.AllHeldFindings()) if finding.finding_id == finding_id)
        self.assertEqual(withdrawn.state, style_ledger.Withdrawn(style_ledger.NoDecision(), style_ledger.DeclarationGone(tip)))


    def test_a_filed_rename_whose_built_twin_renamed_its_item_is_built_not_followed(self) -> None:
        ledger = self.ledger()
        finding_id = self.renamed_parent(ledger)
        _ = self.built_finding(ledger, PARENT_FILE, "Parent", "Renamed")
        _ = self.commit_tip(ledger, {PARENT_FILE: PARENT.replace("Parent", "Renamed")})
        renamed, = self.follow_approved(ledger, [finding_id])
        self.assertEqual((type(renamed), renamed.finding.finding_id), (style_ledger.DeclarationRenamed, finding_id))
        built = next(finding for finding in ledger.held_findings(style_ledger.AllHeldFindings()) if finding.finding_id == finding_id)
        self.assertEqual(built.state, style_ledger.Built(self.root / "decision.md"))


    def test_a_rename_follow_whose_built_rename_loses_its_approval_before_the_lock_stays_filed(self) -> None:
        ledger = self.ledger()
        finding_id = self.restructured_parent(ledger)
        _ = self.built_finding(ledger, PARENT_FILE, "Parent", "Renamed")
        _ = self.commit_tip(ledger, {PARENT_FILE: PARENT.replace("Parent", "Renamed")})
        # The answer is approved at the plan, then not at the recheck under the write lock, nor after.
        answers = iter((True,))
        self.assert_gone_from_file(ledger, finding_id, lambda _: next(answers, False))


    def test_a_qualified_built_rename_of_another_types_method_does_not_follow_onto_a_same_named_method(self) -> None:
        ledger = self.ledger()
        finding_id = self.restructured_method(ledger)
        _ = self.built_finding(ledger, MOVED_FILE, "Other::new", "Other::create")
        # Parent::new is gone, and impl Parent gains an unrelated create.
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub struct Parent;\n\nimpl Parent {\n    pub fn create(seed: u32) -> u32 {\n        seed\n    }\n}\n",
                                     MOVED_FILE: "pub struct Other;\n\nimpl Other {\n    pub fn create() {}\n}\n"})
        self.assert_left(ledger, finding_id, declarations.Declaration("fn", "new", declarations.ImplOrTraitItem("Parent")))


    def test_a_qualified_built_rename_of_another_types_method_does_not_stop_the_owner_rename_follow(self) -> None:
        ledger = self.ledger()
        finding_id = self.restructured_method(ledger)
        _ = self.built_finding(ledger, PARENT_FILE, "Parent", "Renamed")
        _ = self.built_finding(ledger, MOVED_FILE, "Other::new", "Other::create")
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub struct Renamed;\n\nimpl Renamed {\n    pub fn new(seed: u32) -> u32 {\n        seed\n    }\n}\n",
                                     MOVED_FILE: "pub struct Other;\n\nimpl Other {\n    pub fn create() {}\n}\n"})
        filed = ledger.held_findings(style_ledger.HeldFindingsInState("filed"))[0]
        moved, = self.follow_approved(ledger, [finding_id])
        self.assertEqual(moved, style_ledger.DeclarationMoved(
            filed, declarations.Declaration("fn", "new", declarations.ImplOrTraitItem("Renamed")), PARENT_FILE, 5))


    def test_a_built_variant_rename_of_the_same_name_does_not_stop_the_type_rename_follow(self) -> None:
        ledger = self.ledger()
        finding_id = self.restructured_parent(ledger)
        _ = self.built_finding(ledger, PARENT_FILE, "Parent", "Renamed")
        _ = self.built_finding(ledger, MOVED_FILE, "Kind::Parent", "Kind::Child")
        _ = self.commit_tip(ledger, {PARENT_FILE: PARENT.replace("Parent", "Renamed"), MOVED_FILE: "pub enum Kind {\n    Child,\n}\n"})
        filed = ledger.held_findings(style_ledger.HeldFindingsInState("filed"))[0]
        moved, = self.follow_approved(ledger, [finding_id])
        self.assertEqual(moved, style_ledger.DeclarationMoved(
            filed, declarations.Declaration("struct", "Renamed", declarations.ModuleItem()), PARENT_FILE, 2))


    def test_a_filed_method_rename_whose_impl_type_a_built_rename_renamed_is_built_at_the_renamed_owner(self) -> None:
        ledger = self.ledger()
        finding_id, filed = self.renamed_method(ledger, "new")
        renamed, = self.follow_approved(ledger, [finding_id])
        self.assertEqual(renamed, style_ledger.DeclarationRenamed(
            filed, declarations.Declaration("fn", "old", declarations.ImplOrTraitItem("Parent")),
            declarations.Declaration("fn", "new", declarations.ImplOrTraitItem("Renamed")), PARENT_FILE, 4))
        built = next(finding for finding in ledger.held_findings(style_ledger.AllHeldFindings()) if finding.finding_id == finding_id)
        self.assertEqual(built.state, style_ledger.Built(self.root / "decision.md"))


    def test_a_filed_method_rename_not_yet_applied_follows_its_impl_type_rename_and_stays_filed(self) -> None:
        ledger = self.ledger()
        finding_id, filed = self.renamed_method(ledger, "old")
        moved, = self.follow_approved(ledger, [finding_id])
        self.assertEqual(moved, style_ledger.DeclarationMoved(
            filed, declarations.Declaration("fn", "old", declarations.ImplOrTraitItem("Renamed")), PARENT_FILE, 4))
        followed = next(finding for finding in ledger.held_findings(style_ledger.AllHeldFindings()) if finding.finding_id == finding_id)
        self.assertEqual(followed.state, style_ledger.Filed(self.root / "decision.md"))


    def test_a_filed_finding_whose_renamed_item_moved_into_another_crate_is_left(self) -> None:
        ledger = self.ledger()
        finding_id = self.restructured_parent(ledger)
        _ = self.built_finding(ledger, PARENT_FILE, "Parent", "Renamed")
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub fn other() {}\n", "crates/beta/src/other.rs": PARENT.replace("Parent", "Renamed")})
        filed = ledger.held_findings(style_ledger.HeldFindingsInState("filed"))[0]
        self.assert_rename_left(ledger, finding_id, style_ledger.DeclaredInAnotherCrate(
            filed, declarations.Declaration("struct", "Renamed", declarations.ModuleItem()), "crates/beta/src/other.rs"))


    def test_a_method_whose_impl_type_two_built_renames_name_differently_is_left(self) -> None:
        ledger = self.ledger()
        method = "pub enum Parent {\n    A,\n}\n\nimpl Parent {\n    pub fn consume(&self) {}\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: method})
        _, (finding_id,) = self.judge_parent(ledger, ((6, "pub fn consume(&self) {}"),), kind="type")
        _ = self.built_finding(ledger, PARENT_FILE, "enum Parent { A }", "enum First { A }", kind="type")
        _ = self.built_finding(ledger, PARENT_FILE, "enum Parent { A }", "enum Second { A }", kind="type")
        # Both new names are declared, so a step that picked one would follow it.
        _ = self.commit_tip(ledger, {PARENT_FILE: method.replace("Parent", "First") + "\n" + method.replace("Parent", "Second")})
        self.assert_left(ledger, finding_id, declarations.Declaration("fn", "consume", declarations.ImplOrTraitItem("Parent")))


    def test_a_built_type_finding_that_changes_more_than_the_name_gives_no_rename(self) -> None:
        ledger = self.ledger()
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub fn f(x: u8) -> u8 {\n    x\n}\n"})
        _, (finding_id,) = self.judge_parent(ledger, ((2, "x"),), kind="restructure")
        _ = self.built_finding(ledger, PARENT_FILE, "fn f(x: u8)", "fn g(x: u16)", kind="type")
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub fn g(x: u16) -> u16 {\n    x\n}\n"})
        self.assert_left(ledger, finding_id, declarations.Declaration("fn", "f", declarations.ModuleItem()))


    def test_a_finding_whose_item_two_built_renames_renamed_in_turn_follows_the_last_name(self) -> None:
        ledger = self.ledger()
        finding_id = self.restructured_parent(ledger)
        _ = self.built_finding(ledger, PARENT_FILE, "Parent", "Middle")
        _ = self.built_finding(ledger, PARENT_FILE, "Middle", "Renamed")
        _ = self.commit_tip(ledger, {PARENT_FILE: PARENT.replace("Parent", "Renamed")})
        filed = ledger.held_findings(style_ledger.HeldFindingsInState("filed"))[0]
        moved, = self.follow_approved(ledger, [finding_id])
        self.assertEqual(moved, style_ledger.DeclarationMoved(
            filed, declarations.Declaration("struct", "Renamed", declarations.ModuleItem()), PARENT_FILE, 2))


    def test_a_finding_whose_built_renames_come_back_to_its_name_follows_the_name_before_the_return(self) -> None:
        ledger = self.ledger()
        finding_id = self.restructured_parent(ledger)
        _ = self.built_finding(ledger, PARENT_FILE, "Parent", "Middle")
        _ = self.built_finding(ledger, PARENT_FILE, "Middle", "Parent")
        _ = self.commit_tip(ledger, {PARENT_FILE: PARENT.replace("Parent", "Middle")})
        filed = ledger.held_findings(style_ledger.HeldFindingsInState("filed"))[0]
        moved, = self.follow_approved(ledger, [finding_id])
        self.assertEqual(moved, style_ledger.DeclarationMoved(
            filed, declarations.Declaration("struct", "Middle", declarations.ModuleItem()), PARENT_FILE, 2))


    def test_a_filed_method_rename_is_built_when_a_built_rename_renamed_another_type_of_its_owners_name(self) -> None:
        ledger = self.ledger()
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub struct Parent;\n\nimpl Parent {\n    pub fn old() {}\n}\n",
                                     MOVED_FILE: "pub struct Parent;\n"})
        _, (finding_id,) = self.judge_parent(ledger, ((4, "pub fn old() {}"),))
        # The built rename renames the Parent of b.rs only.
        _ = self.built_finding(ledger, MOVED_FILE, "Parent", "Renamed")
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub struct Parent;\n\nimpl Parent {\n    pub fn new() {}\n}\n",
                                     MOVED_FILE: "pub struct Renamed;\n"})
        self.assert_method_rename_built_in_place(ledger, finding_id)


    def test_a_filed_method_rename_is_built_when_a_later_commit_put_back_a_built_rename_of_its_owner(self) -> None:
        ledger = self.ledger()
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub struct Parent;\n\nimpl Parent {\n    pub fn old() {}\n}\n"})
        _, (finding_id,) = self.judge_parent(ledger, ((4, "pub fn old() {}"),))
        _ = self.built_finding(ledger, PARENT_FILE, "Parent", "Renamed")
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub struct Parent;\n\nimpl Parent {\n    pub fn new() {}\n}\n"})
        self.assert_method_rename_built_in_place(ledger, finding_id)


    def test_a_method_whose_self_path_a_built_rename_renamed_follows_the_new_name(self) -> None:
        ledger = self.ledger()
        method = "pub struct Parent;\n\nimpl Parent {\n    pub fn old() {\n        let x = 1;\n    }\n    pub fn caller() {\n        Self::old();\n    }\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: method})
        _, (finding_id,) = self.judge_parent(ledger, ((5, "let x = 1;"),), kind="restructure")
        _ = self.built_finding(ledger, PARENT_FILE, "Self::old", "Self::new", line=8)
        _ = self.commit_tip(ledger, {PARENT_FILE: method.replace("old", "new")})
        filed = ledger.held_findings(style_ledger.HeldFindingsInState("filed"))[0]
        moved, = self.follow_approved(ledger, [finding_id])
        self.assertEqual(moved, style_ledger.DeclarationMoved(
            filed, declarations.Declaration("fn", "new", declarations.ImplOrTraitItem("Parent")), PARENT_FILE, 5))


    def test_a_module_path_rename_of_a_fn_in_another_module_does_not_follow_onto_it(self) -> None:
        ledger = self.ledger()
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub fn helper() {\n    let x = 1;\n}\n", MOVED_FILE: "pub fn helper() {}\n"})
        _, (finding_id,) = self.judge_parent(ledger, ((2, "let x = 1;"),), kind="restructure")
        _ = self.built_finding(ledger, MOVED_FILE, "b::helper", "b::assist")
        # a::helper is gone, and only b::helper has the new name.
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub fn other() {}\n", MOVED_FILE: "pub fn assist() {}\n"})
        self.assert_left(ledger, finding_id, declarations.Declaration("fn", "helper", declarations.ModuleItem()))


    def test_a_module_path_rename_follows_a_fn_in_the_mod_rs_of_that_module(self) -> None:
        ledger = self.ledger()
        module_file = "crates/alpha/src/b/mod.rs"
        function = "pub fn old() {\n    let x = 1;\n}\n"
        _ = self.commit_tip(ledger, {module_file: function})
        _, (finding_id,) = self.judge_parent(ledger, ((2, "let x = 1;"),), kind="restructure", path=module_file)
        _ = self.built_finding(ledger, PARENT_FILE, "b::old", "b::new")
        _ = self.commit_tip(ledger, {module_file: function.replace("fn old", "fn new")})
        filed = ledger.held_findings(style_ledger.HeldFindingsInState("filed"))[0]
        moved, = self.follow_approved(ledger, [finding_id])
        self.assertEqual(moved, style_ledger.DeclarationMoved(
            filed, declarations.Declaration("fn", "new", declarations.ModuleItem()), module_file, 2))


    def test_a_module_qualified_type_rename_follows_an_impl_in_another_file(self) -> None:
        ledger = self.ledger()
        method = "use crate::b::App;\n\nimpl App {\n    pub fn run(&self) {\n        let x = 1;\n    }\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: method, MOVED_FILE: "pub struct App;\n"})
        _, (finding_id,) = self.judge_parent(ledger, ((5, "let x = 1;"),), kind="restructure")
        _ = self.built_finding(ledger, PARENT_FILE, "b::App", "b::Application")
        _ = self.commit_tip(ledger, {PARENT_FILE: method.replace("App", "Application"),
                                     MOVED_FILE: "pub struct Application;\n"})
        filed = ledger.held_findings(style_ledger.HeldFindingsInState("filed"))[0]
        moved, = self.follow_approved(ledger, [finding_id])
        self.assertEqual(moved, style_ledger.DeclarationMoved(
            filed, declarations.Declaration("fn", "run", declarations.ImplOrTraitItem("Application")), PARENT_FILE, 5))


    def test_a_self_qualified_rename_does_not_follow_a_method_of_another_type(self) -> None:
        ledger = self.ledger()
        parent = "pub struct Parent;\n\nimpl Parent {\n    pub fn old() {\n        let x = 1;\n    }\n}\n"
        other = "pub struct Other;\n\nimpl Other {\n    pub fn old() {}\n    pub fn caller() {\n        Self::old();\n    }\n}\n"
        _ = self.commit_tip(ledger, {PARENT_FILE: parent, MOVED_FILE: other})
        _, (finding_id,) = self.judge_parent(ledger, ((5, "let x = 1;"),), kind="restructure")
        _ = self.built_finding(ledger, MOVED_FILE, "Self::old", "Self::new", line=6)
        # Parent loses old and gains an unrelated constructor. The Self path names Other alone.
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub struct Parent;\n\nimpl Parent {\n    pub fn new() -> Self {\n        Self\n    }\n}\n",
                                     MOVED_FILE: other.replace("old", "new")})
        self.assert_left(ledger, finding_id, declarations.Declaration("fn", "old", declarations.ImplOrTraitItem("Parent")))


    def test_a_crate_qualified_rename_follows_a_function_in_the_library_root(self) -> None:
        ledger = self.ledger()
        root_file = "crates/alpha/src/lib.rs"
        function = "pub fn helper() {\n    let x = 1;\n}\n"
        _ = self.commit_tip(ledger, {root_file: function})
        _, (finding_id,) = self.judge_parent(ledger, ((2, "let x = 1;"),), kind="restructure", path=root_file)
        _ = self.built_finding(ledger, PARENT_FILE, "crate::helper", "crate::assist")
        _ = self.commit_tip(ledger, {root_file: function.replace("helper", "assist")})
        filed = ledger.held_findings(style_ledger.HeldFindingsInState("filed"))[0]
        moved, = self.follow_approved(ledger, [finding_id])
        self.assertEqual(moved, style_ledger.DeclarationMoved(
            filed, declarations.Declaration("fn", "assist", declarations.ModuleItem()), root_file, 2))


    def test_a_missing_item_with_no_built_rename_skips_the_rename_follow(self) -> None:
        ledger = self.ledger()
        finding_id = self.restructured_parent(ledger)
        _ = self.commit_tip(ledger, {PARENT_FILE: "pub fn other() {}\n"})
        before = self.held_rows(ledger.database)
        with mock.patch.object(ledger, "_followed_rename", side_effect=AssertionError("no built rename to follow")):
            left, = self.follow_approved(ledger, [finding_id])
        self.assertIsInstance(left, style_ledger.OutsideRenameUnresolved)
        self.assertEqual(self.held_rows(ledger.database), before)



if __name__ == "__main__":
    _ = unittest.main()
