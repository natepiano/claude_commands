"""Approved file renames preserve declarations, comments and unrelated code."""

from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import cast, final
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import approved_renames
import style_footprint
import style_plan
import style_proposals
import proposal_issue

import rules
import style_ledger

from style_plan_test_support import StylePlanFixture


@final
class StylePlanTests(StylePlanFixture):
    def test_approved_proposal_precedes_coverage_and_restart_rebuild_keeps_authorization(self) -> None:
        rule = self.rule("read")
        self.sync()
        folder = self.root / "issues"
        folder.mkdir()
        issue = folder / "approved.md"
        with style_ledger.open_ledger(self.repo) as ledger:
            blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == rule)
            finding = ledger.hold(rule, self.sources[0], 1, "value", "type", "u32", "u64", blob, "outside")
            ledger.file_held(finding.finding_id, issue)
        _ = issue.write_text('---\nstatus: open\nstage: backlog\ntags: [issue, proposal, style]\n'
                             + f'unit: names-unit\nplan: "{self.plan} Phase 1"\nstyle_rule: {rule}\nstyle_crate: crates/alpha\nstyle_rule_blob: {blob}\n---\n')
        _ = self.doc.write_text(self.doc.read_text().replace("## Units", f"- **Proposals:** {folder}\n\n## Units"))
        def start(*args: str) -> None:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(proposal_issue.main(list(args)), 0)
        with mock.patch.object(style_proposals, "proposal_command", side_effect=start):
            self.assertEqual(self.run_next()[0], 0)
            ticket = self.ledger().ticket_open("names-unit")[0]
            self.assertEqual(ticket["kind"], "proposal")
            self.assertEqual(ticket["files"], [self.sources[0]])
            first = self.plan.read_text()
            self.assertIn(f"**Goal:**\nApply the approved findings of `{issue}` in 1 file of `crates/alpha`.\n", first)
            self.assertIn("This ticket builds the approved proposal", first)
            self.assertNotIn("Judge", first)
            self.assertNotIn("overrides the mechanical-only invariant", first)
            self.assertIn("Apply the approved findings.", first)
            self.assertNotIn("Apply only mechanical fixes", first)
            self.assertNotIn("Every structure finding is held as a proposal", first)
            self.assertIn("style_plan.py gate --repo . --ticket names-unit-1", first)
            _ = self.plan.write_text(self.plan_header())
            result = self.run_next()
            self.assertEqual(result[0], 0, result)
            self.assertIn("resume names-unit-1", result[1])
            self.assertEqual(self.plan.read_text(), first)
        self.assertIn("stage: active", issue.read_text())

    def test_binding_gate_reports_an_unapplied_rename_from_head_until_its_scope_is_changed(self) -> None:
        source = "fn f() {\n    let old = input;\n    consume(old);\n}\n"
        ticket, (identity,) = self.approved_binding_ticket(source, ((2, "old", "fresh"),))
        expected = f"review: {identity}: old -> fresh not applied in {self.sources[0]}\n"
        self.assertEqual(self.reference_gate(ticket), (1, expected))
        self.write(self.sources[0], source.replace("old", "fresh"))
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def test_binding_gate_checks_all_pairs_together_from_saved_base_after_head_changes(self) -> None:
        source = "fn f() {\n    let old = input;\n    let second = old;\n    consume(second);\n}\n"
        ticket, ids = self.approved_binding_ticket(source, ((2, "old", "fresh"), (3, "second", "next")), save_base=True)
        self.write(self.sources[0], source.replace("old", "fresh"))
        _ = self.commit()
        self.assertEqual(self.reference_gate(ticket),
                         (1, f"review: {ids[1]}: second -> next not applied in {self.sources[0]}\n"))
        self.write(self.sources[0], source.replace("old", "fresh").replace("second", "next"))
        with mock.patch.object(style_plan, "binding_rename_check", wraps=approved_renames.binding_rename_check) as proof:
            self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))
            self.assertEqual(proof.call_count, 1)
            self.assertEqual(len(cast(tuple[approved_renames.BindingRename, ...], proof.call_args.args[3])), 2)

    def test_binding_gate_keeps_identical_pairs_in_different_paths_separate(self) -> None:
        source = "fn f() {\n    let old = input;\n    consume(old);\n}\n"
        for path in self.sources[:2]:
            self.write(path, source)
        _ = self.commit()
        rename = approved_renames.BindingRename(2, source.splitlines()[1], "old", "fresh")
        self.write(self.sources[0], source.replace("old", "fresh"))
        checks = style_plan.binding_worktree_checks(self.repo, {p: (rename,) for p in self.sources[:2]}, "")
        self.assertEqual(checks[self.sources[0]][rename], approved_renames.AppliedInCode())
        self.assertEqual(checks[self.sources[1]][rename], approved_renames.RenameNotApplied(self.sources[1]))

    def test_approved_file_rename_allows_its_mod_line_and_module_paths_in_other_files(self) -> None:
        ticket = self.file_rename_ticket()
        self.renamed_ledger()
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def test_approved_file_rename_refuses_another_edit_in_a_file_it_changes(self) -> None:
        ticket = self.file_rename_ticket()
        self.renamed_ledger()
        handle = self.repo / "crates/alpha/src/ledger/handle.rs"
        for other in (("pub fn take(", "pub fn take_lock("), ("// Takes the lock.", "// Takes the mutation_lock."),
                      ("    self::lock();", "    self::mutation_lock();"), ("handle.lock;", "handle.mutation_lock;")):
            with self.subTest(other=other):
                renamed = handle.read_text()
                _ = handle.write_text(renamed.replace(*other))
                result, output = self.reference_gate(ticket)
                self.assertEqual(result, 1, output)
                self.assertIn("crates/alpha/src/ledger/handle.rs (change is not an approved rename reference)", output)
                _ = handle.write_text(renamed)

    def test_canonical_code_drops_a_trailing_comma_only_where_it_does_not_change_the_code(self) -> None:
        def words(text: str) -> tuple[style_footprint.CodePiece, ...]:
            spans = style_footprint.token_spans(text)
            return style_footprint.canonical_code(tuple(span.token.value for span in spans),
                                                  style_footprint.comment_gaps(text, spans, lambda line: line))
        # A one-entry tuple is not the grouped expression, even when its entry holds commas.
        self.assertNotEqual(words("let f = (|a: u8, b: u8| a + b,);"), words("let f = (|a: u8, b: u8| a + b);"))
        self.assertNotEqual(words("let m = (HashMap::<u8, u8>::new(),);"), words("let m = (HashMap::<u8, u8>::new());"))
        self.assertEqual(words("acquire(\n    mutation_lock::x,\n);"), words("acquire(mutation_lock::x);"))

    def test_approved_file_rename_refuses_a_comment_moved_between_items(self) -> None:
        handle = (self.LEDGER_HANDLE + "\n// a\nfn first() {}\n\n// b\nfn second() {}\n\n"
                  + "/// c\nfn third() {}\n\n// SAFETY: d\nfn fourth() {}\n")
        ticket = self.file_rename_ticket(files=(("crates/alpha/src/ledger/handle.rs", handle),))
        self.renamed_ledger(handle)
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))
        path = self.repo / "crates/alpha/src/ledger/handle.rs"
        renamed = path.read_text()
        for first, second in (("// a", "// b"), ("/// c", "// SAFETY: d")):
            with self.subTest(first=first, second=second):
                _ = path.write_text(renamed.replace(first, "\0").replace(second, first).replace("\0", second))
                result, output = self.reference_gate(ticket)
                self.assertEqual(result, 1, output)
                self.assertIn("crates/alpha/src/ledger/handle.rs (change is not an approved rename reference)", output)
                _ = path.write_text(renamed)
        # A doc comment that moves to the next item is not in its old place.
        _ = path.write_text(renamed.replace("/// c\nfn third() {}\n\n", "fn third() {}\n\n/// c\n"))
        result, output = self.reference_gate(ticket)
        self.assertEqual(result, 1, output)
        self.assertIn("crates/alpha/src/ledger/handle.rs (change is not an approved rename reference)", output)

    def test_approved_file_rename_keeps_each_doc_comment_with_its_module_item(self) -> None:
        mod_file = "crates/alpha/src/ledger/mod.rs"
        ticket = self.file_rename_ticket(files=(
            (mod_file, self.MODULE_DOCS + self.LEDGER_MOD.replace("mod lock;", "/// The lock.\nmod lock;")),))
        self.renamed_ledger()
        # rustfmt moves the renamed item to its sorted place, with its doc comment.
        renamed = (self.MODULE_DOCS + (self.repo / mod_file).read_text()).replace(
            "mod mutation_lock;", "/// The lock.\nmod mutation_lock;")
        self.write(mod_file, renamed)
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))
        for text in (*(renamed.replace(*edit) for edit in self.MODULE_EDITS),
                     renamed.replace("/// The lock.\n", "").replace("mod map;", "/// The lock.\nmod map;")):
            with self.subTest(text=text):
                self.write(mod_file, text)
                result, output = self.reference_gate(ticket)
                self.assertEqual(result, 1, output)
                self.assertIn(f"{mod_file} (change is not an approved rename reference)", output)

    def test_approved_file_rename_keeps_a_trailing_comma_inside_a_macro_in_another_file(self) -> None:
        handle = self.LEDGER_HANDLE + self.MACRO_COMMAS
        ticket = self.file_rename_ticket(files=(("crates/alpha/src/ledger/handle.rs", handle),))
        self.renamed_ledger(handle)
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))
        path = self.repo / "crates/alpha/src/ledger/handle.rs"
        renamed = path.read_text()
        for comma in ("([1,]) =>", "enabled!([1,])", "#[enabled([1,])]"):
            with self.subTest(comma=comma):
                _ = path.write_text(renamed.replace(comma, comma.replace(",", "")))
                result, output = self.reference_gate(ticket)
                self.assertEqual(result, 1, output)
                self.assertIn("crates/alpha/src/ledger/handle.rs (change is not an approved rename reference)", output)
                _ = path.write_text(renamed)

    def test_approved_file_rename_keeps_a_trailing_comma_inside_a_macro_in_the_renamed_file(self) -> None:
        lock = "pub struct MutationLock;\npub struct LockError;\n" + self.MACRO_COMMAS
        ticket = self.file_rename_ticket(files=(("crates/alpha/src/ledger/lock.rs", lock),))
        self.renamed_ledger()
        new = "crates/alpha/src/ledger/mutation_lock.rs"
        for comma in ("([1,]) =>", "enabled!([1,])", "#[enabled([1,])]"):
            with self.subTest(comma=comma):
                self.write(new, lock.replace(comma, comma.replace(",", "")))
                result, output = self.reference_gate(ticket)
                self.assertEqual(result, 1, output)
                self.assertIn(self.RENAMED_REFUSAL, output)

    def test_an_unchanged_renamed_file_outside_every_crate_root_passes(self) -> None:
        old, new = "crates/alpha/tests/helper.rs", "crates/alpha/tests/test_helper.rs"
        self.write(old, "pub fn helper() {}\n")
        _ = self.commit()
        _ = self.git("mv", old, new)
        moved = approved_renames.FileRename(old, new)
        self.assertTrue(style_plan.approved_renamed_file(self.repo, moved, (moved,), ()))
        self.write(new, "pub fn helper() {}\npub fn other() {}\n")
        self.assertFalse(style_plan.approved_renamed_file(self.repo, moved, (moved,), ()))

    def test_approved_file_rename_allows_a_path_attribute_that_names_the_file(self) -> None:
        tests = "crates/alpha/tests/unit_tests.rs"
        ticket = self.file_rename_ticket(files=((tests, self.UNIT_TESTS),))
        self.renamed_ledger()
        # rustfmt moves the renamed item to its sorted place.
        renamed = ('#[path = "../src/ledger/handle.rs"]\nmod handle;\n#[path = "../src/ledger/map.rs"]\nmod map;\n'
                   + '#[path = "../src/ledger/mutation_lock.rs"]\nmod mutation_lock;\n')
        self.write(tests, renamed)
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))
        for literal in ('"../src/ledger/mutation_locks.rs"', '"../src/mutation_lock.rs"', '"../src/ledger/./mutation_lock.rs"'):
            with self.subTest(literal=literal):
                self.write(tests, renamed.replace('"../src/ledger/mutation_lock.rs"', literal))
                result, output = self.reference_gate(ticket)
                self.assertEqual(result, 1, output)
                self.assertIn(f"{tests} (change is not an approved rename reference)", output)

    def test_approved_file_rename_reads_a_raw_path_attribute(self) -> None:
        tests = "crates/alpha/tests/unit_tests.rs"
        ticket = self.file_rename_ticket(files=((tests, '#[r#path = "../src/ledger/lock.rs"]\nmod lock;\n'),))
        self.renamed_ledger()
        self.write(tests, '#[r#path = "../src/ledger/mutation_lock.rs"]\nmod mutation_lock;\n')
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))

    def test_approved_file_rename_renames_a_raw_module_name_and_keeps_its_prefix(self) -> None:
        mod_file = "crates/alpha/src/ledger/mod.rs"
        ticket = self.file_rename_ticket(files=((mod_file, self.LEDGER_MOD.replace("mod lock;", "mod r#lock;")),))
        self.renamed_ledger()
        renamed = (self.repo / mod_file).read_text().replace("mod mutation_lock;", "mod r#mutation_lock;")
        self.write(mod_file, renamed)
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))
        # The rename changes the name only, so a site that drops its `r#` changes something else.
        self.write(mod_file, renamed.replace("mod r#mutation_lock;", "mod mutation_lock;"))
        result, output = self.reference_gate(ticket)
        self.assertEqual(result, 1, output)
        self.assertIn(f"{mod_file} (change is not an approved rename reference)", output)

    def test_approved_file_rename_refuses_a_file_with_a_path_attribute_it_cannot_read(self) -> None:
        mod_file = "crates/alpha/src/ledger/mod.rs"
        cfg_path = '#[cfg_attr(test, path = "map_tests.rs")]\nmod map_tests;\n'
        ticket = self.file_rename_ticket(files=((mod_file, self.LEDGER_MOD + cfg_path),))
        self.renamed_ledger()
        self.write(mod_file, (self.repo / mod_file).read_text() + cfg_path)
        result, output = self.reference_gate(ticket)
        self.assertEqual(result, 1, output)
        self.assertIn(f"{mod_file} (change is not an approved rename reference)", output)

    def test_approved_file_rename_refuses_a_path_attribute_that_names_another_file(self) -> None:
        tests = "crates/alpha/tests/unit_tests.rs"
        ticket = self.file_rename_ticket(files=((tests, '#[path = "../src/ledger/map.rs"]\nmod lock;\n'),))
        self.renamed_ledger()
        self.write(tests, '#[path = "../src/ledger/map.rs"]\nmod mutation_lock;\n')
        result, output = self.reference_gate(ticket)
        self.assertEqual(result, 1, output)
        self.assertIn(f"{tests} (change is not an approved rename reference)", output)

    def test_approved_file_rename_refuses_a_mod_line_inside_a_macro(self) -> None:
        mod_file = "crates/alpha/src/ledger/mod.rs"
        lib = 'pub mod ledger;\nconst SOURCE: &str = stringify!(#[path = "ledger/lock.rs"] mod lock;);\n'
        macros = "macro_rules! declare {\n    () => {\n        mod lock;\n    };\n}\nconst SOURCE: &str = stringify! { mod lock; };\n"
        ticket = self.file_rename_ticket(files=((self.sources[0], lib), (mod_file, self.LEDGER_MOD + macros)))
        self.renamed_ledger()
        self.write(mod_file, (self.repo / mod_file).read_text() + macros)
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))
        for path, first, second in (
                (self.sources[0], '"ledger/lock.rs"] mod lock;', '"ledger/mutation_lock.rs"] mod mutation_lock;'),
                (mod_file, "        mod lock;", "        mod mutation_lock;"),
                (mod_file, "{ mod lock; }", "{ mod mutation_lock; }")):
            with self.subTest(path=path, second=second):
                text = (self.repo / path).read_text()
                self.write(path, text.replace(first, second))
                result, output = self.reference_gate(ticket)
                self.assertEqual(result, 1, output)
                self.assertIn(f"{path} (change is not an approved rename reference)", output)
                self.write(path, text)

    def test_approved_file_rename_allows_a_crate_path_to_the_module_in_a_comment(self) -> None:
        handle = "//! Holds [`crate::ledger::lock::MutationLock`], next to [`super::lock`].\n" + self.LEDGER_HANDLE
        map_file = "//! Next to [`crate::ledger::lock`] and [`crate::other::lock`].\npub fn lock() {}\n"
        ticket = self.file_rename_ticket(files=(("crates/alpha/src/ledger/handle.rs", handle),
                                                ("crates/alpha/src/ledger/map.rs", map_file)))
        self.renamed_ledger(handle.replace("crate::ledger::lock::", "crate::ledger::mutation_lock::"))
        self.write("crates/alpha/src/ledger/map.rs", map_file.replace("crate::ledger::lock`", "crate::ledger::mutation_lock`"))
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))
        for path, first, second in (
                ("crates/alpha/src/ledger/handle.rs", "//! Holds", "//! Keeps"),
                ("crates/alpha/src/ledger/map.rs", "crate::other::lock", "crate::other::mutation_lock"),
                ("crates/alpha/src/ledger/handle.rs", "[`super::lock`]", "[`super::mutation_lock`]")):
            with self.subTest(path=path, second=second):
                text = (self.repo / path).read_text()
                self.write(path, text.replace(first, second))
                result, output = self.reference_gate(ticket)
                self.assertEqual(result, 1, output)
                self.assertIn(f"{path} (change is not an approved rename reference)", output)
                self.write(path, text)

    def test_approved_file_rename_reads_a_plain_path_from_its_module_or_the_library_name(self) -> None:
        store, beta = "crates/alpha/src/store/mod.rs", self.sources[3]
        ticket = self.file_rename_ticket(files=(
            (self.sources[0], "pub mod ledger;\nuse ledger::lock::MutationLock;\n"),
            (self.sources[2], "use alpha::ledger::lock::MutationLock;\n"),
            (store, "use archive::ledger::lock::Entry;\n"),
            (beta, "use beta::ledger::lock::MutationLock;\n")))
        self.renamed_ledger()
        for path in (self.sources[0], self.sources[2]):
            self.write(path, (self.repo / path).read_text().replace("::lock::", "::mutation_lock::"))
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))
        for path in (store, beta):
            with self.subTest(path=path):
                text = (self.repo / path).read_text()
                self.write(path, text.replace("::lock::", "::mutation_lock::"))
                result, output = self.reference_gate(ticket)
                self.assertEqual(result, 1, output)
                self.assertIn(f"{path} (change is not an approved rename reference)", output)
                self.write(path, text)

    def test_approved_file_rename_and_item_rename_edit_one_file(self) -> None:
        handle = self.LEDGER_HANDLE.replace("-> lock::LockError", "-> super::lock::LockError")
        ticket = self.file_rename_ticket(files=(("crates/alpha/src/ledger/handle.rs", handle),),
                                         pairs=(("lock::LockError", "lock::LockFailure"),))
        self.renamed_ledger(handle, "LockFailure")
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))
        path = self.repo / "crates/alpha/src/ledger/handle.rs"
        _ = path.write_text(path.read_text().replace("super::mutation_lock::LockFailure", "super::mutation_lock::LockFault"))
        result, output = self.reference_gate(ticket)
        self.assertEqual(result, 1, output)
        self.assertIn("crates/alpha/src/ledger/handle.rs (change is not an approved rename reference)", output)

    def test_approved_file_rename_leaves_a_folder_of_child_modules_to_a_person(self) -> None:
        folder = "crates/alpha/src/ledger/lock"
        ticket = self.file_rename_ticket(files=(
            (f"{folder}.rs", "mod guard;\npub struct MutationLock;\npub struct LockError;\n"),
            (f"{folder}/guard.rs", "pub struct Guard;\n")))
        _ = self.git("mv", folder, "crates/alpha/src/ledger/mutation_lock")
        self.renamed_ledger()
        result, output = self.reference_gate(ticket)
        self.assertEqual(result, 1, output)
        for name in ("lock", "mutation_lock"):
            self.assertIn(f"crates/alpha/src/ledger/{name}/guard.rs (in crates/alpha/src/ledger/{name}/, the folder of "
                          + "a renamed module: a person moves it)", output)

    def test_approved_file_rename_allows_only_the_rename_in_the_renamed_file(self) -> None:
        new = "crates/alpha/src/ledger/mutation_lock.rs"
        ticket = self.file_rename_ticket(files=(("crates/alpha/src/ledger/lock.rs", self.LOCK),))
        self.renamed_ledger()
        renamed = self.LOCK.replace("crate::ledger::lock::", "crate::ledger::mutation_lock::")
        # rustfmt can order the use lines again; that moves no definition between files.
        for text in (renamed, renamed.replace("use std::fmt;\nuse crate::ledger::handle::Handle;",
                                              "use crate::ledger::handle::Handle;\nuse std::fmt;")):
            with self.subTest(text=text):
                self.write(new, text)
                self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))
        for first, second in (("pub struct LockError;\n", "pub struct LockError;\npub struct Guard;\n"),
                              ("pub struct LockError;\n", ""),
                              ("/// The lock a mutation takes.", "/// The lock each mutation takes.")):
            with self.subTest(second=second):
                self.write(new, renamed.replace(first, second))
                result, output = self.reference_gate(ticket)
                self.assertEqual(result, 1, output)
                self.assertIn(self.RENAMED_REFUSAL, output)

    def test_approved_file_rename_keeps_each_doc_comment_with_its_module_item_in_the_renamed_file(self) -> None:
        lock = self.MODULE_DOCS + "pub struct MutationLock;\npub struct LockError;\n"
        ticket = self.file_rename_ticket(files=(("crates/alpha/src/ledger/lock.rs", lock),))
        self.renamed_ledger()
        new = "crates/alpha/src/ledger/mutation_lock.rs"
        self.write(new, lock.replace(self.MODULE_DOCS, "#[enabled({1,})]\npub mod beta;\n/// Alpha API.\n"
                                                       + "#[enabled(alpha)]\npub mod alpha;\n"))
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))
        for edit in self.MODULE_EDITS:
            with self.subTest(edit=edit):
                self.write(new, lock.replace(*edit))
                result, output = self.reference_gate(ticket)
                self.assertEqual(result, 1, output)
                self.assertIn(self.RENAMED_REFUSAL, output)

    def test_approved_file_rename_leaves_the_renamed_file_free_when_another_finding_names_it(self) -> None:
        self.assertEqual(self.renamed_declaration_gate((("lock::LockError", "lock::LockFailure"),)),
                         (0, "approved proposal\n"))

    def test_approved_file_rename_refuses_a_declaration_rename_that_no_finding_names(self) -> None:
        result, output = self.renamed_declaration_gate(())
        self.assertEqual(result, 1, output)
        self.assertIn(self.RENAMED_REFUSAL, output)

    def test_approved_file_rename_refuses_a_copy_or_a_removed_file(self) -> None:
        old, new = "crates/alpha/src/ledger/lock.rs", "crates/alpha/src/ledger/mutation_lock.rs"
        ticket = self.file_rename_ticket()
        self.renamed_ledger()
        self.assertEqual(self.reference_gate(ticket), (0, "approved proposal\n"))
        text = (self.repo / new).read_text()
        self.write(old, text)
        result, output = self.reference_gate(ticket)
        self.assertEqual(result, 1, output)
        self.assertIn(self.RENAMED_REFUSAL, output)
        (self.repo / old).unlink()
        (self.repo / new).unlink()
        result, output = self.reference_gate(ticket)
        self.assertEqual(result, 1, output)
        self.assertIn(self.RENAMED_REFUSAL, output)

if __name__ == "__main__":
    _ = unittest.main()
