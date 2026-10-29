"""Approved item renames permit only their reference and prose edits."""

from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import final
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import style_plan

import mechanical_classes
import rules

from style_plan_test_support import StylePlanFixture


@final
class StylePlanTests(StylePlanFixture):
    def test_approved_rename_allows_only_reference_edits_in_an_outside_file(self) -> None:
        before = ('// Outcome::OldName stays in this comment.\r\n'
                  + 'fn read() { /* nested /* note */ comment */ Outcome :: OldName; '
                  + 'let text = r#"Outcome::OldName é"#; }\r\n')
        self.write(self.sources[1], before)
        _ = self.commit()
        ticket = self.approved_reference_ticket()
        self.write(self.sources[0], "// The finding file keeps unrestricted approval.\nenum Outcome { NewName }\n")
        self.write(self.sources[1], before.replace("Outcome :: OldName", "Outcome :: NewName"))
        _ = self.git("add", self.sources[1])
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def test_approved_rename_allows_two_pairs_and_qualified_use_leaves(self) -> None:
        before = ("use crate::m::Outcome::{OldName, Other};\n"
                  + "use crate::m::Outcome::SignalFailed;\n"
                  + "fn read() { Outcome::OldName; Outcome::SignalFailed; }\n")
        self.write(self.sources[1], before)
        _ = self.commit()
        ticket = self.approved_reference_ticket((("Outcome::OldName", "Outcome::NewName"),
                                                 ("Outcome::SignalFailed", "Outcome::Failed")))
        self.write(self.sources[1], before.replace("OldName", "NewName").replace("SignalFailed", "Failed"))
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def test_approved_rename_updates_comments_without_code_changes(self) -> None:
        before = ("// OldName and Outcome::OldName\r\n"
                  + "/// See OldName and Outcome::OldName.\r\n"
                  + "//! OldName remains; Outcome::OldName changes.\r\n"
                  + "/* OldName /* Outcome::OldName */ OldName */\r\n"
                  + "/** OldName */\r\n")
        self.write(self.sources[1], before)
        _ = self.commit()
        ticket = self.approved_reference_ticket()
        after = before.replace("OldName", "NewName").replace("//! NewName", "//! OldName")
        self.write(self.sources[1], after)
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def test_approved_rename_updates_code_and_comments_together(self) -> None:
        before = "// OldName and Outcome::OldName\nfn read() { Outcome::OldName; }\n"
        self.write(self.sources[1], before)
        _ = self.commit()
        ticket = self.approved_reference_ticket()
        self.write(self.sources[1], before.replace("OldName", "NewName"))
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def test_approved_rename_refuses_other_comment_changes_and_partial_words(self) -> None:
        before = "// OldName OldNameX XOldName _OldName OldName_ 1OldName OldName1\n"
        self.write(self.sources[1], before)
        _ = self.commit()
        ticket = self.approved_reference_ticket()
        for after in (before.replace("OldName ", "NewName ", 1) + "// extra\n",
                      before.replace("OldNameX", "NewNameX"),
                      before.replace("XOldName", "XNewName"),
                      before.replace("_OldName", "_NewName"),
                      before.replace("OldName_", "NewName_"),
                      before.replace("1OldName", "1NewName"),
                      before.replace("OldName1", "NewName1")):
            with self.subTest(after=after):
                self.write(self.sources[1], after)
                result, output = self.reference_gate(ticket)
                self.assertEqual(result, 1, output)

    def test_approved_rename_keeps_string_and_char_literals_unchanged(self) -> None:
        before = 'fn read() { let text = "OldName"; let raw = r#"Outcome::OldName"#; let c = \'A\'; }\n'
        self.write(self.sources[1], before)
        _ = self.commit()
        ticket = self.approved_reference_ticket()
        for after in (before.replace('"OldName"', '"NewName"'),
                      before.replace("Outcome::OldName", "Outcome::NewName"),
                      before.replace("'A'", "'B'")):
            with self.subTest(after=after):
                self.write(self.sources[1], after)
                result, output = self.reference_gate(ticket)
                self.assertEqual(result, 1, output)

    def test_approved_rename_updates_only_whole_names_in_markdown(self) -> None:
        path = "docs/renames.md"
        before = "# Outcome::OldName\r\nUse OldName; OldName may stay. OldNameX is unrelated. é\r\n"
        self.write(path, before)
        _ = self.commit()
        ticket = self.approved_reference_ticket()
        after = before.replace("Outcome::OldName", "Outcome::NewName").replace("Use OldName", "Use NewName")
        self.write(path, after)
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))
        for refused in (after.replace("may stay", "can stay"),
                        after.replace("OldNameX", "NewNameX"), after.replace("\r\n", "\n")):
            with self.subTest(after=refused):
                self.write(path, refused)
                result, output = self.reference_gate(ticket)
                self.assertEqual(result, 1, output)
                self.assertIn(path, output)

    def test_approved_rename_refuses_reference_edits_in_other_file_types(self) -> None:
        paths = ("notes.txt", "settings.toml")
        for path in paths:
            self.write(path, "Outcome::OldName\n")
        _ = self.commit()
        ticket = self.approved_reference_ticket()
        for path in paths:
            with self.subTest(path=path):
                self.write(path, "Outcome::NewName\n")
                result, output = self.reference_gate(ticket)
                self.assertEqual(result, 1, output)
                self.assertIn(path, output)
                self.write(path, "Outcome::OldName\n")

    def test_approved_rename_ticket_requires_comment_and_markdown_updates(self) -> None:
        _ = self.approved_reference_ticket()
        self.write_approved_phase()
        sentence = ("For each of these renames, find every use through the compiler, not a text search, and update each one "
                    + "in other files: `Outcome::OldName` -> `Outcome::NewName`. "
                    + "Also update every comment and Markdown doc in the repo that means the "
                    + "renamed item, and leave mentions of other items with the same name. "
                    + "Change nothing else in those files.")
        self.assertIn("Apply the approved findings. " + sentence, self.plan.read_text())

    def test_a_proposal_phase_lists_each_widened_path_under_its_files(self) -> None:
        ticket = self.approved_reference_ticket()
        extra = "crates/alpha/src/extra.rs"
        with redirect_stdout(io.StringIO()):
            self.assertEqual(style_plan.main(["widen", "--repo", str(self.repo), "--ticket", ticket, "--files", extra,
                                              "--reason", "the move needs a new file", "--by", "showrunner"]), 0)
        self.write_approved_phase()
        at = self.ledger().ticket_open("names-unit")[0]["widened"][0]["widened_at"]
        self.assertIn(f"**Files:**\n- `{self.sources[0]}`\n"
                      + f"- widened: {extra} — the move needs a new file (showrunner, {at})\n", self.plan.read_text())

    def test_a_proposal_phase_names_each_qualified_pair_and_keeps_a_bare_rename_in_its_own_file(self) -> None:
        _ = self.approved_reference_ticket((("Outcome::OldName", "Outcome::NewName"), ("helper", "assistant")))
        self.write_approved_phase()
        text = self.plan.read_text()
        self.assertIn("update each one in other files: `Outcome::OldName` -> `Outcome::NewName`. ", text)
        self.assertNotIn("`helper`", text)
        self.assertIn("Apply any other rename only in its own file; when other files use its old name, hold it. ", text)

    def test_a_proposal_phase_with_only_qualified_pairs_has_no_own_file_rename_line(self) -> None:
        _ = self.approved_reference_ticket()
        self.write_approved_phase()
        self.assertNotIn("Apply any other rename only in its own file", self.plan.read_text())

    def test_nonrename_approval_ticket_omits_comment_and_markdown_instruction(self) -> None:
        _ = self.approved_reference_ticket(kinds=("type",))
        self.write_approved_phase()
        self.assertNotIn("find every use through the compiler", self.plan.read_text())

    def test_a_proposal_phase_lists_only_the_cells_record_takes_and_leaves_out_a_tier_b_cell_with_no_listed_site(self) -> None:
        rule = self.rule("partial", "tier: B\nfamily: names\ncandidates:\n  kind: regex\n  pattern: absent\n  surface: partial\n"
                         + "  uncovered: Read everything else.\n")
        uncovered = rule + "#uncovered"
        self.sync()
        issue = self.root / "partial-approval.md"
        ledger = self.ledger()
        blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == rule)
        ids = tuple(ledger.hold(held_rule, self.sources[0], line, "rename", "rename", "Outcome::Old", "Outcome::New", blob,
                                "outside").finding_id for line, held_rule in enumerate((rule, uncovered), start=1))
        for finding_id in ids:
            ledger.file_held(finding_id, issue)
        _ = issue.write_text("---\nstatus: open\ntags: [proposal, style]\nunit: names-unit\nplan: approved\n"
                             + f"style_rule: {rule}\nstyle_crate: crates/alpha\nstyle_rule_blob: {blob}\n---\n")
        ledger.ticket_add("names-unit-1", "names-unit", "proposal", "names", "crates/alpha", [self.sources[0]],
                          [rule, uncovered], proposal=str(issue), finding_ids=ids)
        ticket = ledger.queued_tickets("names-unit")[0]
        ledger.ticket_assign(ticket["id"], "demo-style-names")
        style_plan.write_phase(ledger, ticket, self.plan, self.plan_header())
        text = self.plan.read_text()
        self.assertIn("`style_ledger.py record --ticket names-unit-1 --rule <rule> --path <file>`", text)
        self.assertIn(f"- `{uncovered}` · `{self.sources[0]}`", text)
        self.assertNotIn(f"- `{rule}` · `{self.sources[0]}`", text)
        self.assertNotIn("Record every judged cell", text)

    def test_a_proposal_phase_gives_no_cell_at_a_ticket_file_that_record_refuses_for_its_rule(self) -> None:
        rule = self.rule("approved-cells")
        self.write("docs/notes.md", "Outcome::Old\n")
        self.tip = self.commit()
        self.sync()
        issue = self.root / "cells-approval.md"
        ledger = self.ledger()
        blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == rule)
        ids = tuple(ledger.hold(rule, path, 1, "rename", "rename", "Outcome::Old", "Outcome::New", blob, "outside").finding_id
                    for path in (self.sources[0], "docs/notes.md"))
        for finding_id in ids:
            ledger.file_held(finding_id, issue)
        _ = issue.write_text("---\nstatus: open\ntags: [proposal, style]\nunit: names-unit\nplan: approved\n"
                             + f"style_rule: {rule}\nstyle_crate: crates/alpha\nstyle_rule_blob: {blob}\n---\n")
        ledger.ticket_add("names-unit-1", "names-unit", "proposal", "names", "crates/alpha", [self.sources[0], "docs/notes.md"],
                          [rule], proposal=str(issue), finding_ids=ids)
        ticket = ledger.queued_tickets("names-unit")[0]
        ledger.ticket_assign(ticket["id"], "demo-style-names")
        style_plan.write_phase(ledger, ticket, self.plan, self.plan_header())
        text = self.plan.read_text()
        self.assertIn(f"- `{rule}` · `{self.sources[0]}`", text)
        self.assertNotIn(f"- `{rule}` · `docs/notes.md`", text)

    def test_prose_rename_approval_ticket_omits_reference_update_instruction(self) -> None:
        _ = self.approved_reference_ticket((("The file contains MutationLock.",
                                             "Rename ledger/lock.rs to ledger/mutation_lock.rs."),))
        self.write_approved_phase()
        self.assertIn("Apply the approved findings.", self.plan.read_text())
        self.assertNotIn("find every use through the compiler", self.plan.read_text())

    def test_bare_rename_mention_ignores_unknown_tokens_without_the_old_name(self) -> None:
        self.write(self.sources[0], "enum Outcome { OldName, Other }\n")
        self.write(self.sources[2], "fn unrelated() { § }\n")
        self.write("docs/renames.md", "Use OldName.\n")
        _ = self.commit()
        ticket = self.approved_reference_ticket()
        self.write("docs/renames.md", "Use NewName.\n")
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def test_bare_rename_mention_allows_no_matching_rust_files(self) -> None:
        self.write(self.sources[2], "fn unrelated() { § }\n")
        self.write("docs/renames.md", "Use OldName.\n")
        _ = self.commit()
        ticket = self.approved_reference_ticket()
        self.write("docs/renames.md", "Use NewName.\n")
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def test_bare_rename_mention_refuses_unknown_tokens_in_a_matching_file(self) -> None:
        self.write(self.sources[0], "enum Outcome { OldName, Other }\n")
        self.write(self.sources[2], "fn read() { Outcome::OldName; § }\n")
        self.write("docs/renames.md", "Use OldName.\n")
        _ = self.commit()
        ticket = self.approved_reference_ticket()
        self.write("docs/renames.md", "Use NewName.\n")
        result, output = self.reference_gate(ticket)
        self.assertEqual(result, 1, output)
        self.assertIn("docs/renames.md", output)

    def test_read_ticket_omits_comment_and_markdown_instruction(self) -> None:
        _ = self.rule("read")
        self.sync()
        self.assertEqual(self.run_next()[0], 0)
        self.assertNotIn("find every use through the compiler", self.plan.read_text())

    def test_approved_rename_allows_bare_comment_mentions_of_a_unique_item(self) -> None:
        self.write(self.sources[0], "enum Outcome { OldName, Other }\nimpl Outcome { fn read() { Self::OldName; } }\n")
        self.write(self.sources[1], "use crate::Outcome::{OldName};\n// OldName and Outcome::OldName\n")
        _ = self.commit()
        ticket = self.approved_reference_ticket()
        self.write(self.sources[1], "use crate::Outcome::{OldName};\n// NewName and Outcome::OldName\n")
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def test_approved_rename_refuses_ambiguous_bare_comments_but_allows_qualified_mentions(self) -> None:
        self.write(self.sources[0], "enum Outcome { OldName, Other }\n")
        self.write(self.sources[2], "enum Different { OldName, Other }\n")
        self.write(self.sources[1], "// OldName and Outcome::OldName\n")
        _ = self.commit()
        ticket = self.approved_reference_ticket()
        self.write(self.sources[1], "// NewName and Outcome::OldName\n")
        self.assertEqual(self.reference_gate(ticket)[0], 1)
        self.write(self.sources[1], "// OldName and Outcome::NewName\n")
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def test_approved_rename_allows_bare_markdown_mentions_of_a_unique_item(self) -> None:
        self.write(self.sources[0], "enum Outcome { OldName, Other }\n")
        self.write("docs/renames.md", "Use OldName and Outcome::OldName.\n")
        _ = self.commit()
        ticket = self.approved_reference_ticket()
        self.write("docs/renames.md", "Use NewName and Outcome::OldName.\n")
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def test_approved_rename_refuses_ambiguous_bare_markdown_but_allows_qualified_mentions(self) -> None:
        self.write(self.sources[0], "enum Outcome { OldName, Other }\n")
        self.write(self.sources[2], "enum Different { OldName, Other }\n")
        self.write("docs/renames.md", "Use OldName and Outcome::OldName.\n")
        _ = self.commit()
        ticket = self.approved_reference_ticket()
        self.write("docs/renames.md", "Use NewName and Outcome::OldName.\n")
        self.assertEqual(self.reference_gate(ticket)[0], 1)
        self.write("docs/renames.md", "Use OldName and Outcome::NewName.\n")
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def test_approved_rename_checks_ambiguity_at_head_even_in_the_finding_file(self) -> None:
        self.write(self.sources[0], "enum Outcome { OldName }\nenum Different { OldName }\n")
        self.write("docs/renames.md", "Use OldName.\n")
        _ = self.commit()
        ticket = self.approved_reference_ticket()
        self.write(self.sources[0], "enum Outcome { NewName }\n")
        self.write("docs/renames.md", "Use NewName.\n")
        self.assertEqual(self.reference_gate(ticket)[0], 1)

    def test_approved_rename_uses_the_parent_of_an_absolute_approved_path(self) -> None:
        self.write(self.sources[1], "fn read() { library::Outcome::OldName; }\n")
        _ = self.commit()
        ticket = self.approved_reference_ticket((("::library::Outcome::OldName", "::library::Outcome::NewName"),))
        self.write(self.sources[1], "fn read() { library::Outcome::NewName; }\n")
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def test_approved_rename_refuses_other_text_changes_in_reference_files(self) -> None:
        before = ('// Outcome::OldName\nfn read() { Outcome::OldName; let n = 1; '
                  + 'let text = "Outcome::OldName"; }\n')
        self.write(self.sources[1], before)
        _ = self.commit()
        ticket = self.approved_reference_ticket()
        reference = before.replace("{ Outcome::OldName", "{ Outcome::NewName")
        for after in (reference.replace("n = 1", "n = 2"),
                      reference.replace("// Outcome::OldName", "// Outcome::NewName extra"),
                      reference.replace('"Outcome::OldName"', '"Outcome::NewName"'),
                      reference.replace("let n", "let  n"),
                      before.replace("\n", "\r\n")):
            with self.subTest(after=after):
                self.write(self.sources[1], after)
                result, output = self.reference_gate(ticket)
                self.assertEqual(result, 1, output)
                self.assertIn("files outside approved findings:", output)
                self.assertIn(self.sources[1] + " (change is not an approved rename reference)", output)

    def test_approved_proposal_gate_allows_the_files_of_a_pure_move_and_only_them(self) -> None:
        ticket = self.approved_reference_ticket(rule_name="module-structure")
        lib, other, extra = self.sources[2], self.sources[3], "crates/beta/src/extra.rs"
        limit = "/// The most rows.\npub(crate) const LIMIT: usize = 4;\n"
        text = "\npub fn text() -> &'static str { stringify!(a+b) }\n"
        self.write(lib, "mod extra;\nmod other;\n\npub fn value() -> usize { crate::other::LIMIT }\n" + text)
        self.write(other, limit + "\npub(crate) fn read() -> usize { 1 }\n")
        self.write(extra, "pub(crate) fn more() -> usize { 2 }\n")
        _ = self.commit()
        moved = "mod constants;\nmod extra;\nmod other;\n\npub fn value() -> usize { crate::constants::LIMIT }\n" + text
        self.write(lib, moved)
        self.write(other, "pub(crate) fn read() -> usize { 1 }\n")
        self.write("crates/beta/src/constants.rs", limit)
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))
        constants = "crates/beta/src/constants.rs"
        # The proof reads code without its spacing, and judge also reads the spacing inside a macro.
        for path, edit, refused in ((lib, moved.replace("LIMIT }", "LIMIT + 1 }"), (lib, other, constants)),
                                    (lib, moved.replace("a+b", "a + b"), (lib, other, constants)),
                                    (extra, "pub(crate) fn more() -> usize {  2 }\n", (extra,))):
            with self.subTest(edit=edit):
                self.write(path, edit)
                result, output = self.reference_gate(ticket)
                self.assertEqual(result, 1, output)
                self.assertIn("files outside approved findings:", output)
                for name in (lib, other, constants, extra):
                    self.assertEqual(name + " (change" in output, name in refused, output)
                self.write(lib, moved)
                self.write(extra, "pub(crate) fn more() -> usize { 2 }\n")

    def test_approved_proposal_gate_allows_a_stale_lint_removal_and_holds_an_added_allow_or_code(self) -> None:
        ticket = self.approved_reference_ticket(rule_name="test-module-allow-boilerplate")
        other = self.sources[3]
        tests = "#[cfg(test)]\n#[allow(clippy::panic, clippy::unwrap_used)]\nmod tests {\n    fn example() {}\n}\n"
        self.write(other, "pub fn read() -> usize { 1 }\n\n" + tests)
        _ = self.commit()
        removed = "pub fn read() -> usize { 1 }\n\n" + tests.replace("clippy::panic, ", "")
        self.write(other, removed)
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))
        for edit in (removed.replace("clippy::unwrap_used", "clippy::unwrap_used, clippy::expect_used"),
                     removed.replace("#[cfg(test)]", "#[allow(dead_code)]\n#[cfg(test)]"),
                     removed.replace("{ 1 }", "{ 2 }")):
            with self.subTest(edit=edit):
                self.write(other, edit)
                result, output = self.reference_gate(ticket)
                self.assertEqual(result, 1, output)
                self.assertIn(f"files outside approved findings: {other} (change", output)

    def test_approved_proposal_gate_refuses_a_lint_removal_for_a_ticket_of_another_class(self) -> None:
        ticket = self.approved_reference_ticket(rule_name="module-structure")
        tests = "#[cfg(test)]\n#[allow(clippy::panic, clippy::unwrap_used)]\nmod tests {\n    fn example() {}\n}\n"
        self.write(self.sources[3], "pub fn read() -> usize { 1 }\n\n" + tests)
        _ = self.commit()
        self.write(self.sources[3], "pub fn read() -> usize { 1 }\n\n" + tests.replace("clippy::panic, ", ""))
        result, output = self.reference_gate(ticket)
        self.assertEqual(result, 1, output)
        self.assertIn(f"files outside approved findings: {self.sources[3]} (change", output)

    def test_approved_proposal_gate_reports_a_refused_class_list_as_exit_two(self) -> None:
        ticket = self.approved_reference_ticket()
        self.write(self.sources[3], "pub fn moved() -> usize { 1 }\n")
        refused = mechanical_classes.ClassListError("class pure-move names the unknown proof copy")
        with mock.patch.object(mechanical_classes, "classes", side_effect=refused):
            self.assertEqual(self.reference_gate(ticket), (2, "error: class pure-move names the unknown proof copy\n"))

    def test_approved_proposal_gate_allows_a_widened_file_and_only_it(self) -> None:
        ticket = self.approved_reference_ticket()
        outside, other = self.sources[3], self.sources[2]
        self.write(outside, "pub fn moved() -> usize { 1 }\n")
        result, output = self.reference_gate(ticket)
        self.assertEqual(result, 1, output)
        refusal = "files outside approved findings: {} (change is not an approved rename reference)"
        self.assertIn(refusal.format(outside), output)
        widened = io.StringIO()
        with redirect_stdout(widened):
            self.assertEqual(style_plan.main(["widen", "--repo", str(self.repo), "--ticket", ticket, "--files", outside,
                                              "--reason", "the move needs a new file", "--by", "showrunner"]), 0)
        line = widened.getvalue()
        self.assertTrue(line.startswith(f"widened: {outside} — the move needs a new file (showrunner, ")
                        and line.endswith(")\n") and line.count("\n") == 1, line)
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))
        self.write(other, "pub fn other() -> usize { 2 }\n")
        result, output = self.reference_gate(ticket)
        self.assertEqual(result, 1, output)
        self.assertIn(refusal.format(other) + "\n", output)
        self.assertNotIn(outside, output)

if __name__ == "__main__":
    _ = unittest.main()
