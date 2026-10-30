"""Statement boundaries may contain expressions, but never hide item ownership."""

from __future__ import annotations

import unittest
from typing import final, override

import finding_sites
import style_ledger
import test_style_ledger_sites
from ledger_test_support import PARENT_FILE


@final
class FindingSitesTests(unittest.TestCase):
    @classmethod
    @override
    def setUpClass(cls) -> None:
        super().setUpClass()
        # Ledger fixtures clone a template initialized by their class setup.
        cls.addClassCleanup(test_style_ledger_sites.StyleLedgerSitesTests.doClassCleanups)
        test_style_ledger_sites.StyleLedgerSitesTests.setUpClass()

    def assert_unproved_owner(self, source: str) -> None:
        sites = finding_sites.SourceSites(source)
        if isinstance(sites.item_reading, finding_sites.UnreadableSourceItems):
            return
        with self.assertRaises(finding_sites.UnreadableSourceItems):
            for line in range(1, len(sites.lines) + 1):
                _ = sites.owner_chain(line)

    def assert_hidden_item_refused(self, item: str) -> None:
        for prefix in ("unrecognized", 'unrecognized pub(crate) const unsafe extern "C"', "async unrecognized"):
            with self.subTest(prefix=prefix):
                source = "fn container() { " + prefix + " " + item + " }"
                self.assert_unproved_owner(source)

    def test_refuses_fn_behind_qualifier(self) -> None:
        self.assert_hidden_item_refused("fn work() {}")

    def test_refuses_struct_behind_qualifier(self) -> None:
        self.assert_hidden_item_refused("struct Work { a: u8 }")

    def test_refuses_enum_behind_qualifier(self) -> None:
        self.assert_hidden_item_refused("enum Work { A }")

    def test_refuses_trait_behind_qualifier(self) -> None:
        self.assert_hidden_item_refused("trait Work {}")

    def test_refuses_impl_behind_qualifier(self) -> None:
        self.assert_hidden_item_refused("impl Work {}")

    def test_refuses_mod_behind_qualifier(self) -> None:
        self.assert_hidden_item_refused("mod work {}")

    def test_refuses_static_behind_qualifier(self) -> None:
        self.assert_hidden_item_refused("static WORK: u8 = 1;")

    def test_refuses_type_behind_qualifier(self) -> None:
        self.assert_hidden_item_refused("type Work = u8;")

    def test_refuses_use_behind_qualifier(self) -> None:
        self.assert_hidden_item_refused("use crate::Work;")

    def test_refuses_extern_behind_qualifier(self) -> None:
        self.assert_hidden_item_refused('extern "C" { fn work(); }')

    def test_refuses_const_item_behind_qualifier(self) -> None:
        self.assert_hidden_item_refused("const WORK: u8 = 1;")

    def test_refuses_union_behind_qualifier(self) -> None:
        self.assert_hidden_item_refused("union Work { a: u8 }")

    def test_refuses_macro_rules_behind_qualifier(self) -> None:
        self.assert_hidden_item_refused("macro_rules! work { () => {} }")

    def assert_macro_prefix_refused(self, prefix: str) -> None:
        source = "macro_rules! declarations { () => { " + prefix + " fn first() { fn work() {} } }; }"
        # F003: parsing the unrelated file is allowed; macro input itself is opaque.
        with self.assertRaises(finding_sites.UnreadableSourceItems):
            _ = finding_sites.SourceSites(source).owner_chain(1)

    def test_refuses_dollar_visibility_prefix(self) -> None:
        self.assert_macro_prefix_refused("$vis")

    def test_refuses_unknown_qualifier_chain(self) -> None:
        self.assert_macro_prefix_refused('unrecognized another pub(crate) unsafe extern "C"')

    def test_refuses_two_identifier_prefix(self) -> None:
        self.assert_macro_prefix_refused("alpha beta")

    def test_refuses_macro_fragment_prefix(self) -> None:
        self.assert_macro_prefix_refused("$name:ident")

    def test_refuses_path_prefix(self) -> None:
        self.assert_macro_prefix_refused("unknown::modifier")

    def test_refuses_unrecognized_expression_start(self) -> None:
        for expression in ("@ unknown;", "# unknown;", "= unknown;", "=> unknown;", "$name:ident;"):
            with self.subTest(expression=expression):
                self.assert_unproved_owner("fn work() { " + expression + " }")

    def test_expression_qualifiers_do_not_hide_items(self) -> None:
        for expression in ("async { x };", "unsafe { x };", "const { x };", "async move || x;",
                           "async unsafe { x };", "const move || x;", "default;", "safe;", "auto;"):
            with self.subTest(expression=expression):
                reading = finding_sites.SourceSites("fn work() { " + expression + " }").item_reading
                self.assertIsInstance(reading, tuple)

    def test_expression_keywords_and_weak_item_names_remain_expressions(self) -> None:
        for expression in ("let union = 1;", "return union;", "macro_rules();"):
            with self.subTest(expression=expression):
                reading = finding_sites.SourceSites("fn work() { " + expression + " }").item_reading
                self.assertIsInstance(reading, tuple)

    def test_reserved_keyword_boundary_refuses(self) -> None:
        for word in ("abstract", "become", "do", "final", "override", "priv", "typeof", "unsized", "virtual"):
            with self.subTest(word=word):
                self.assert_unproved_owner("fn work() { " + word + " x; }")

    def assert_owner_move_refused(self, prefix: str) -> None:
        fixture = test_style_ledger_sites.StyleLedgerSitesTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        work = "        fn work() {\n            let old = 791;\n        }\n"
        source = ("fn container() {\n    " + prefix + " fn first() {\n" + work
                  + "    }\n    " + prefix + " fn second() {}\n}\n")
        ledger, _, finding = fixture.site_fixture(source=source, line=4)
        moved = ("fn container() {\n    " + prefix + " fn first() {}\n    " + prefix
                 + " fn second() {\n" + work + "    }\n}\n")
        tip = fixture.commit_tip(ledger, {PARENT_FILE: moved})
        self.assertIsInstance(fixture.strict_site(ledger, finding, tip), style_ledger.NeedsJudgment)
        self.assertIsInstance(ledger.finding_site(finding, tip), style_ledger.NeedsJudgment)

    def test_generic_prefix_cannot_hide_owner_move(self) -> None:
        self.assert_owner_move_refused("unknown another::<T>")

    def test_async_move_prefix_cannot_hide_owner_move(self) -> None:
        self.assert_owner_move_refused("unknown async move")

    def test_parenthesized_prefix_cannot_hide_owner_move(self) -> None:
        self.assert_owner_move_refused("unknown (marker)")

    def test_macro_call_prefix_cannot_hide_owner_move(self) -> None:
        self.assert_owner_move_refused("unknown call!()")

    def test_turbofish_prefix_cannot_hide_owner_move(self) -> None:
        self.assert_owner_move_refused("unknown::<T>")

    def test_bracket_prefix_cannot_hide_owner_move(self) -> None:
        self.assert_owner_move_refused("unknown [fragment]")

    def test_closed_qualifiers_record_every_grammar_form(self) -> None:
        prefixes = (
            "", "#[cfg(test)]", "/// Documentation\n", "pub", "pub(crate)", "pub(self)", "pub(super)",
            "pub(in crate::nested)", "default", "const", "async", "unsafe", "extern", 'extern "C"',
            '#[allow(dead_code)] pub(in self::nested) default const async unsafe extern "C"',
        )
        for prefix in prefixes:
            with self.subTest(prefix=prefix):
                source = "fn container() {\n" + prefix + " fn work() {}\n}\n"
                items = finding_sites.SourceSites(source).items
                self.assertIn(("fn container", "fn work"), tuple(item.identity for item in items))
                # The same qualifier grammar cannot bless a preceding unknown form.
                hidden = "fn container() { unknown::<T> " + prefix + " fn work() {} }"
                self.assert_unproved_owner(hidden)

    def test_closed_qualifiers_refuse_wrong_order_or_visibility(self) -> None:
        for prefix in ("unsafe async", "async const", 'extern "C" unsafe', "pub(unknown)", "safe", "auto"):
            with self.subTest(prefix=prefix):
                source = "fn container() { " + prefix + " fn work() {} }"
                self.assert_unproved_owner(source)

    def test_every_item_keyword_requires_closed_qualifiers(self) -> None:
        for item in ("fn work() {}", "struct Work {}", "enum Work {}", "union Work { a: u8 }",
                     "trait Work {}", "impl Work {}", "mod work {}", "type Work = u8;",
                     "const WORK: u8 = 1;", "static WORK: u8 = 1;", "macro_rules! work { () => {} }",
                     "use crate::Work;", "extern crate work;"):
            with self.subTest(item=item):
                readable = finding_sites.SourceSites("fn container() { " + item + " }")
                self.assertIsInstance(readable.item_reading, tuple)
                self.assertGreater(len(readable.items), 1)
                hidden = finding_sites.SourceSites("fn container() { unknown::<T> " + item + " }")
                self.assert_unproved_owner(hidden.source)

    def test_macro_opacity_is_confined_to_its_body(self) -> None:
        fixture = test_style_ledger_sites.StyleLedgerSitesTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        source = ("macro_rules! identity {\n    ($x:expr) => { $x };\n}\n"
                  "fn work() {\n    let old = 791;\n}\n")
        ledger, _, finding = fixture.site_fixture(source=source, line=5)
        tip = fixture.commit_tip(ledger, {PARENT_FILE: "// heading\n" + source})
        expected = style_ledger.UnchangedSite(PARENT_FILE, 6)
        self.assertEqual(fixture.strict_site(ledger, finding, tip), expected)
        self.assertEqual(ledger.finding_site(finding, tip), expected)
        old, new = finding_sites.SourceSites(source), finding_sites.SourceSites("// heading\n" + source)
        with self.assertRaises(finding_sites.UnreadableSourceItems):
            _ = old.same_owner(2, new, 3)
        # A well-formed macro with no metavariables is equally opaque at its own sites.
        with self.assertRaises(finding_sites.UnreadableSourceItems):
            _ = finding_sites.SourceSites("macro_rules! item { () => { fn work() {} } }").owner_chain(1)

    def test_macro_site_refuses_even_without_metavariables(self) -> None:
        fixture = test_style_ledger_sites.StyleLedgerSitesTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        source = "macro_rules! declarations { () => {\n    fn work() {\n        let old = 791;\n    }\n}; }\n"
        ledger, _, finding = fixture.site_fixture(source=source, line=3)
        tip = fixture.commit_tip(ledger, {PARENT_FILE: "// heading\n" + source})
        self.assertIsInstance(fixture.strict_site(ledger, finding, tip), style_ledger.NeedsJudgment)
        self.assertIsInstance(ledger.finding_site(finding, tip), style_ledger.NeedsJudgment)

    def assert_continuation_carries(self, expression: str) -> None:
        fixture = test_style_ledger_sites.StyleLedgerSitesTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        fixture.assert_expression_boundary_carries(expression)
        # Check both a normal item after the continuation and an unknown prefix.
        source = "fn container() { " + expression + " fn nested() {} }"
        items = finding_sites.SourceSites(source).items
        self.assertIn(("fn container", "fn nested"), tuple(item.identity for item in items))
        hidden = "fn container() { " + expression + " unknown::<T> fn hidden() {} }"
        self.assert_unproved_owner(hidden)

    # Reduced from the real cargo-liner contexts in phases/90/loss_census.md.
    def test_enum_variant_brace_comma_continues(self) -> None:
        self.assert_continuation_carries("enum Answer { Scope { edge_id: EdgeId }, Sequence { blocker: Id }, }")

    def test_match_arm_brace_comma_continues(self) -> None:
        self.assert_continuation_carries("match outcome { Ok(x) => { consume(x); }, Err(e) => report(e), };")

    def test_destructuring_brace_arrow_continues(self) -> None:
        self.assert_continuation_carries("match request { Request::Run { coordination_run_id } => Some(coordination_run_id), _ => None };")

    def test_destructuring_brace_equals_continues(self) -> None:
        self.assert_continuation_carries("let ExportRequest { name, path } = request;")

    def test_destructuring_let_else_continues(self) -> None:
        self.assert_continuation_carries("let UseBinding::Named { name, path } = binding else { return; };")

    def test_block_expression_else_continues(self) -> None:
        self.assert_continuation_carries("let Some(value) = { next() } else { return; };")

    def test_unsafe_block_postfix_question_continues(self) -> None:
        self.assert_continuation_carries("let bytes = unsafe { pixels.as_slice() }?;")

    def test_block_postfix_dot_continues(self) -> None:
        self.assert_continuation_carries("let bytes = unsafe { pixels.as_slice() }.len();")

    def test_attribute_tests_let_after_metadata(self) -> None:
        self.assert_continuation_carries("#[cfg(test)] let value = next();")

    def test_attribute_tests_if_after_metadata(self) -> None:
        self.assert_continuation_carries('#[allow(clippy::cast_precision_loss, reason = "display-only")] if value > 1 { draw(); }')

    def test_attribute_tests_block_after_metadata(self) -> None:
        self.assert_continuation_carries("#[cfg(test)] { draw(); }")

    def test_attribute_tests_call_after_metadata(self) -> None:
        self.assert_continuation_carries("#[cfg(test)] draw();")

    def test_attribute_tests_field_after_metadata(self) -> None:
        self.assert_continuation_carries("let pair = Pair { #[cfg(test)] a: 1, b: 2 };")

    def test_qualified_self_path_starts_expression(self) -> None:
        self.assert_continuation_carries("<Self as crate::Action>::ALL;")

    def test_qualified_reference_path_starts_expression(self) -> None:
        self.assert_continuation_carries("<&str>::try_from(properties.get(key)?);")

    def test_binding_at_struct_pattern_continues(self) -> None:
        self.assert_continuation_carries("match resolution { resolved @ ImportResolution::Resolved { .. } => consume(resolved), _ => () };")

    def test_struct_fields_have_field_boundaries(self) -> None:
        self.assert_continuation_carries("struct CachedRepoData { pub repo: Repo, pub(crate) data: Data, }")

    def test_struct_literal_shorthand_fields_have_field_boundaries(self) -> None:
        self.assert_continuation_carries("let data = CachedRepoData { repo: owner_repo.clone(), data, ..previous };")

    def test_variant_fields_and_shorthand_patterns_have_field_boundaries(self) -> None:
        self.assert_continuation_carries("match value { Event::Ready { repo, data } => { consume(data); }, Event::Waiting => wait(), };")

    def test_generic_and_closure_commas_do_not_start_statements(self) -> None:
        self.assert_continuation_carries("let f = |a: A, mut b: B| { consume(a, &mut b); }; let x = Map::<A, dyn Trait>::new();")

    def test_unreadable_sibling_owner_does_not_poison_known_owner(self) -> None:
        source = ("fn unreadable() {\n    unknown::<T> fn hidden() {}\n}\n"
                  "fn work() {\n    let old = 791;\n}\n")
        old, new = finding_sites.SourceSites(source), finding_sites.SourceSites("// heading\n" + source)
        self.assertIsInstance(old.item_reading, tuple)
        self.assertTrue(old.same_owner(5, new, 6))
        with self.assertRaises(finding_sites.UnreadableSourceItems):
            _ = old.owner_chain(2)
        self.assertEqual(old.owner_chain(5), (("fn work",),))

    def test_unreadable_nested_owner_does_not_poison_sibling(self) -> None:
        source = ("mod container {\n    fn unreadable() { unknown::<T> fn hidden() {} }\n"
                  "    fn work() {\n        let old = 791;\n    }\n}\n")
        old, new = finding_sites.SourceSites(source), finding_sites.SourceSites("// heading\n" + source)
        self.assertIsInstance(old.item_reading, tuple)
        self.assertTrue(old.same_owner(4, new, 5))
        with self.assertRaises(finding_sites.UnreadableSourceItems):
            _ = old.owner_chain(2)

    def test_same_file_unreadable_owner_still_refuses(self) -> None:
        fixture = test_style_ledger_sites.StyleLedgerSitesTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        source = "fn bad() {\n    let old = 791;\n    unknown::<T> fn hidden() {}\n}\n"
        ledger, _, finding = fixture.site_fixture(source=source, line=2)
        tip = fixture.commit_tip(ledger, {"crates/alpha/src/other.rs": "fn unrelated() {}\n"})
        with self.subTest(proof="strict"):
            self.assertIsInstance(fixture.strict_site(ledger, finding, tip), style_ledger.NeedsJudgment)
        with self.subTest(proof="public"):
            self.assertIsInstance(ledger.finding_site(finding, tip), style_ledger.NeedsJudgment)

    def test_same_file_safe_owner_carries_despite_unreadable_sibling(self) -> None:
        fixture = test_style_ledger_sites.StyleLedgerSitesTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        source = "fn bad() { unknown::<T> fn hidden() {} }\nfn work() {\n    let old = 791;\n}\n"
        ledger, _, finding = fixture.site_fixture(source=source, line=3)
        tip = fixture.commit_tip(ledger, {"crates/alpha/src/other.rs": "fn unrelated() {}\n"})
        self.assertEqual(fixture.strict_site(ledger, finding, tip),
                         style_ledger.UnchangedSite(PARENT_FILE, 3))
        self.assertEqual(ledger.finding_site(finding, tip),
                         style_ledger.UnchangedSite(PARENT_FILE, 3))


if __name__ == "__main__":
    _ = unittest.main()
