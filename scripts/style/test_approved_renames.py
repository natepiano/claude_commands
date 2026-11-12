"""Decide from a file's names and declarations whether an approved rename is in the code."""

from __future__ import annotations

import unittest

from scripts.style import style_footprint
from scripts.style import approved_renames
from scripts.style.approved_renames import (AppliedCheck, AppliedInCode, ApprovedRename, BindingProof, BindingRename, BindingScope,
                              Declarations, FileReader, FileRename, Folder, MissingFile, NoAppliedCheck, NoBindingRename,
                              NoBindingScope, NoFileRename, RenameNotApplied, UncertainScan, applied_check,
                              approved_rename, binding_rename, binding_rename_check, binding_scope, resolution_scope, declaration_sites, file_rename,
                              refactor_rename, rename_declaration, RenameDeclaration,
                              pattern_binding_check, PatternBindingCollision, DistinctPatternBindings)


def files(sources: dict[str, str]) -> FileReader:
    def read(path: str) -> bytes | Folder | MissingFile:
        if path in sources:
            return sources[path].encode()
        return Folder() if any(source.startswith(path + "/") for source in sources) else MissingFile()
    return read


def check(before: str, after: str, path: str, source: str) -> AppliedCheck:
    return applied_check("rename", before, after, path, files({path: source}))


def sites(before: str, after: str, path: str, source: str, name: str) -> Declarations | UncertainScan:
    rename = approved_rename("rename", before, after)
    assert isinstance(rename, ApprovedRename)
    return declaration_sites(tuple(token.value for token in style_footprint.tokens(source)), path, rename, name)



# label, complete item(s), target line, refusal kind. Independent items are outside the policy region.
PRESENCE_REFUSALS = (
    ("signature", "fn label(checkout_info: usize) {\nmap(|g| g + 1)\n}\n", 2, approved_renames.OuterBindingBlocked),
    ("break closure one", "fn label() { loop { break |checkout_info: usize| {\n[1].map(|g| g + checkout_info)\n}; } }\n", 2, approved_renames.OuterBindingUnproved),
    ("break closure two", "fn label() { loop { break |unused: usize, checkout_info: usize| {\n[1].map(|g| g + checkout_info)\n}; } }\n", 2, approved_renames.OuterBindingUnproved),
    ("attribute", "#[inject] fn label() {\nmap(|g| g + 1)\n}\n", 2, approved_renames.OuterBindingUnproved),
    ("enclosing attribute", "#[inject] mod outer { fn label() {\nmap(|g| g + 1)\n} }\n", 2, approved_renames.OuterBindingUnproved),
    ("proc bang", "fn label() { inject!();\nmap(|g| g + 1)\n}\n", 2, approved_renames.OuterBindingUnproved),
    ("use", "fn label() { use dep::checkout_info;\nmap(|g| g + 1)\n}\n", 2, approved_renames.OuterBindingUnproved),
    ("shorthand", "fn label() { consume(Value { checkout_info });\nmap(|g| g + 1)\n}\n", 2, approved_renames.OuterBindingUnproved),
    ("impl const generic", "impl<const checkout_info: usize> Value<checkout_info> { fn label() {\nmap(|g| g + 1)\n} }\n", 2, approved_renames.OuterBindingUnproved),
    ("unread target", "fn label() { loop {\nbreak |g: usize| g + 1;\n} }\n", 2, approved_renames.OuterBindingUnproved),
    ("const initializer", "const VALUE: fn(usize) -> usize = { use dep::checkout_info;\n|g| g + 1\n};\n", 2, approved_renames.OuterBindingUnproved),
    ("else arm", "fn label() { if let Some(checkout_info) = if flag { value } else { other } {} else {\nmap(|g| g + 1)\n} }\n", 2, approved_renames.OuterBindingUnproved),
)

# Enclosing brace ownership is independent of the item's Rust kind.
INHERITED_INNER_ATTRIBUTES = (
    ("impl", "impl X { #![inject] fn label() {\nmap(|g| g + 1)\n} }\n"),
    ("trait", "trait X { #![inject] fn label() {\nmap(|g| g + 1)\n} }\n"),
    ("function", "fn label() { #![inject]\nmap(|g| g + 1)\n}\n"),
    ("block", "fn label() { { #![inject]\nmap(|g| g + 1)\n} }\n"),
)

IDENTICAL_BINDING_ITEMS = "fn first() {\n    map(|g| g + 1)\n}\nfn second() {\n    map(|g| g + 1)\n}\n"
DUPLICATE_BINDING_ITEM_PATH = "impl X { fn first() {\n    map(|g| g + 1)\n} }\nimpl X { fn first() {\n    map(|g| g + 1)\n} }\n"
CAPTURE_BINDING_ITEM = 'fn label() {\n    let g = 1;\n    println!("{g}");\n}\n'

# Each target is local despite its enclosing field declaration; line two owns g.
FIELD_EXPRESSION_BINDINGS = (
    ("array length", "struct X { f: [u8; { let checkout_info=2;\nlet g: usize=1; g }] }\n"),
    ("const generic", "struct X { f: Value<{ let checkout_info=2;\nlet g: usize=1; g }> }\n"),
    ("variant field", "enum X { V { f: [u8; { let checkout_info=2;\nlet g: usize=1; g }] } }\n"),
    ("field closure", "struct X { f: [u8; { let checkout_info=2;\nmap(|g: usize| g + 1) }] }\n"),
)
TRUE_FIELD_BINDING = "struct X {\n    g: usize,\n}\n"

# The keyword `const` declares a parameter here, never an item; line two owns g.
GENERIC_PARAMETER_BINDINGS = (
    ("fn const parameter", "fn label<const checkout_info: usize,\nconst g: usize>() {}\n"),
    ("impl const parameter", "impl<const checkout_info: usize,\nconst g: usize> X<checkout_info, g> {}\n"),
    ("struct const parameter", "struct X<const checkout_info: usize,\nconst g: usize> {}\n"),
    ("trait const parameter", "trait X<const checkout_info: usize,\nconst g: usize> {}\n"),
)
TRUE_ITEM_BINDINGS = (
    "pub const g: usize = 1;\n",
    "#[allow(dead_code)] pub(crate) fn g() {}\n",
    "struct g;\n", "enum g {}\n", "union g { value: usize }\n",
    "trait g {}\n", "type g = usize;\n", "static mut g: usize = 1;\n",
    "mod g {}\n", "macro_rules! g { () => {} }\n",
    "fn outer() { const g: usize = 1; }\n",
    "mod outer { pub const g: usize = 1; }\n",
    "impl X { const g: usize = 1; }\n", "trait X { const g: usize = 1; }\n",
)

# The shared reader proves each function's header before encountering unread body syntax.
UNREAD_ITEM_BODIES = (
    ("async move block", "// Header remains readable.\nfn g() { let task = async move { 1 }; }\n"),
    ("typed closure", "// Header remains readable.\nfn g() { let task = || -> usize { 1 }; }\n"),
)

# Local admission must not reuse a readable header as proof of either body.
UNREAD_LOCAL_BODIES = tuple((label, source.replace("fn g()", "fn label()").replace("; }", "; map(|g| g + 1) }"))
                            for label, source in UNREAD_ITEM_BODIES)

# Current signatures determine blockers; the anchor only establishes target identity.
PARAMETER_CHANGES = (
    ("removed", "fn label(checkout_info: usize) {\n    map(|g| g + 1)\n}\n",
     "fn label() {\n    map(|g| g + 1)\n}\n", False),
    ("added", "fn label() {\n    map(|g| g + 1)\n}\n",
     "fn label(checkout_info: usize) {\n    map(|g| g + 1)\n}\n", True),
)

class AppliedCheckTests(unittest.TestCase):
    def test_only_current_parameters_can_block_an_anchored_local_rename(self) -> None:
        for label, source, current, blocked in PARAMETER_CHANGES:
            for applied in (False, True):
                with self.subTest(parameter=label, applied=applied):
                    pair = BindingRename(2, source.splitlines()[1], "g", "checkout_info")
                    authority = approved_renames.AnchoredRenameSource("judged", "src/lib.rs", 2, source)
                    edited = current.replace("|g| g", "|checkout_info| checkout_info") if applied else current
                    result = approved_renames.admit_anchored_rename(authority, "row", pair, "src/lib.rs", edited)
                    if blocked:
                        self.assertIsInstance(result, approved_renames.OuterBindingBlocked)
                        assert isinstance(result, approved_renames.OuterBindingBlocked)
                        self.assertEqual(result.line, 1)
                        self.assertEqual(result.text, current.splitlines()[0])
                    else:
                        self.assertIsInstance(result, approved_renames.AdmittedLocalRename)
                        assert isinstance(result, approved_renames.AdmittedLocalRename)
                        self.assertTrue(result.covers("row", pair, "src/lib.rs", edited))
                        self.assertFalse(result.covers("row", pair, "src/lib.rs", source))

    def test_signature_changes_cannot_map_a_local_rename_inside_an_unread_body(self) -> None:
        from scripts.style import finding_sites
        for label, source in UNREAD_LOCAL_BODIES:
            with self.subTest(body=label):
                item, = finding_sites.SourceSites(source).items
                self.assertIsInstance(item.header_reading, finding_sites.ReadItemHeader)
                self.assertIsInstance(item.body_reading, finding_sites.UnreadItemBody)
                self.assertFalse(hasattr(item.body_reading, "tokens"))
                pair = BindingRename(2, source.splitlines()[1], "g", "checkout_info")
                authority = approved_renames.AnchoredRenameSource("judged", "src/lib.rs", 2, source)
                pending = source.replace("label()", "label(extra: usize)")
                for current in (source, pending, pending.replace("|g| g", "|checkout_info| checkout_info")):
                    with self.subTest(current=current):
                        result = approved_renames.admit_anchored_rename(authority, "row", pair, "src/lib.rs", current)
                        self.assertIsInstance(result, approved_renames.OuterBindingUnproved)
                        assert isinstance(result, approved_renames.OuterBindingUnproved)
                        self.assertEqual(result.reason, "containing item body unread")
                read_item, = finding_sites.SourceSites("fn label() { map(|g| g + 1) }").items
                self.assertIsInstance(read_item.body_reading, finding_sites.ReadItemBody)
                assert isinstance(read_item.body_reading, finding_sites.ReadItemBody)
                self.assertTrue(read_item.body_reading.tokens)

    def test_proved_item_names_keep_their_exemption_with_unread_bodies(self) -> None:
        from scripts.style import finding_sites
        for label, source in UNREAD_ITEM_BODIES:
            with self.subTest(body=label):
                item, = finding_sites.SourceSites(source).items
                self.assertIsInstance(item.header_reading, finding_sites.ReadItemHeader)
                self.assertIsInstance(item.body_reading, finding_sites.UnreadItemBody)
                pair = BindingRename(2, source.splitlines()[1], "g", "checkout_info")
                authority = approved_renames.AnchoredRenameSource("judged", "src/lib.rs", 2, source)
                self.assertIsInstance(approved_renames.admit_anchored_rename(authority, "row", pair, "src/lib.rs", source),
                                      approved_renames.NonLocalReferenceCompanion)
                with self.assertRaises(finding_sites.UnreadableSourceItems):
                    _ = finding_sites.SourceSites(source).owner_chain(2)
        source = "fn g(value: impl Trait { fn hidden() {} }) {}\n"
        item, = finding_sites.SourceSites(source).items
        self.assertIsInstance(item.header_reading, finding_sites.UnreadItemHeader)
        pair = BindingRename(1, source.strip(), "g", "checkout_info")
        authority = approved_renames.AnchoredRenameSource("judged", "src/lib.rs", 1, source)
        self.assertIsInstance(approved_renames.admit_anchored_rename(authority, "row", pair, "src/lib.rs", source),
                              approved_renames.OuterBindingUnproved)

    def test_generic_parameters_need_positive_item_position_before_an_exemption(self) -> None:
        for label, source in GENERIC_PARAMETER_BINDINGS:
            with self.subTest(form=label):
                pair = BindingRename(2, source.splitlines()[1], "g", "checkout_info")
                authority = approved_renames.AnchoredRenameSource("judged", "src/lib.rs", 2, source)
                result = approved_renames.admit_anchored_rename(authority, "row", pair, "src/lib.rs", source)
                self.assertIsInstance(result, approved_renames.OuterBindingBlocked | approved_renames.OuterBindingUnproved)
        pair = BindingRename(2, TRUE_FIELD_BINDING.splitlines()[1], "g", "checkout_info")
        authority = approved_renames.AnchoredRenameSource("judged", "src/lib.rs", 2, TRUE_FIELD_BINDING)
        self.assertIsInstance(approved_renames.admit_anchored_rename(
            authority, "row", pair, "src/lib.rs", TRUE_FIELD_BINDING), approved_renames.NonLocalReferenceCompanion)

        for source in TRUE_ITEM_BINDINGS:
            with self.subTest(item=source):
                pair = BindingRename(1, source.splitlines()[0], "g", "checkout_info")
                authority = approved_renames.AnchoredRenameSource("judged", "src/lib.rs", 1, source)
                self.assertIsInstance(approved_renames.admit_anchored_rename(authority, "row", pair, "src/lib.rs", source),
                                      approved_renames.NonLocalReferenceCompanion)

    def test_field_expressions_never_receive_the_nonlocal_field_exemption(self) -> None:
        for label, source in FIELD_EXPRESSION_BINDINGS:
            with self.subTest(form=label):
                pair = BindingRename(2, source.splitlines()[1], "g", "checkout_info")
                authority = approved_renames.AnchoredRenameSource("judged", "src/lib.rs", 2, source)
                result = approved_renames.admit_anchored_rename(authority, "row", pair, "src/lib.rs", source)
                self.assertIsInstance(result, approved_renames.OuterBindingBlocked | approved_renames.OuterBindingUnproved)
        pair = BindingRename(2, TRUE_FIELD_BINDING.splitlines()[1], "g", "checkout_info")
        authority = approved_renames.AnchoredRenameSource("judged", "src/lib.rs", 2, TRUE_FIELD_BINDING)
        self.assertIsInstance(approved_renames.admit_anchored_rename(
            authority, "row", pair, "src/lib.rs", TRUE_FIELD_BINDING), approved_renames.NonLocalReferenceCompanion)

    def assert_admitted_local_capability_is_bound_to_anchor_version_pair_path_and_snapshot(self) -> None:
        from scripts.style.approved_renames import (AdmittedLocalRename, AnchoredRenameSource, RenameAnchorUnavailable,
                                      admit_anchored_rename, OuterBindingBlocked, OuterBindingUnproved)
        source = "fn label() {\n    map(|g| g + 1)\n}\n"
        pair = BindingRename(2, source.splitlines()[1], "g", "checkout_info")
        authority = AnchoredRenameSource("judged", "src/lib.rs", 2, source)
        admitted = admit_anchored_rename(authority, "version-1", pair, "src/lib.rs", source)
        self.assertIsInstance(admitted, AdmittedLocalRename)
        assert isinstance(admitted, AdmittedLocalRename)
        self.assertTrue(admitted.covers("version-1", pair, "src/lib.rs", source))
        self.assertFalse(admitted.covers("version-2", pair, "src/lib.rs", source))
        self.assertFalse(admitted.covers("version-1", pair, "other.rs", source))
        changed = source.replace("label()", "label(checkout_info: usize)")
        self.assertFalse(admitted.covers("version-1", pair, "src/lib.rs", changed))
        self.assertIsInstance(admit_anchored_rename(authority, "version-1", pair, "src/lib.rs", changed), OuterBindingBlocked)
        self.assertIsInstance(admit_anchored_rename(RenameAnchorUnavailable(), "version-1", pair, "src/lib.rs", source), OuterBindingUnproved)

    def test_current_admission_maps_the_recorded_item_instead_of_an_identical_line(self) -> None:
        self.assert_admitted_local_capability_is_bound_to_anchor_version_pair_path_and_snapshot()
        source = IDENTICAL_BINDING_ITEMS
        rename = BindingRename(2, source.splitlines()[1], "g", "checkout_info")
        edited = source.replace("first()", "first(checkout_info: usize)").replace("|g| g", "|checkout_info| checkout_info", 1)
        result = approved_renames.current_rename_admission(edited, rename, source)
        self.assertIsInstance(result, approved_renames.OuterBindingBlocked)
        assert isinstance(result, approved_renames.OuterBindingBlocked)
        self.assertEqual((result.name, result.line), ("checkout_info", 1))

    def test_current_admission_refuses_a_duplicate_recorded_item_path(self) -> None:
        source = DUPLICATE_BINDING_ITEM_PATH
        rename = BindingRename(2, source.splitlines()[1], "g", "checkout_info")
        edited = source.replace("|g| g", "|checkout_info| checkout_info", 1)
        result = approved_renames.current_rename_admission(edited, rename, source)
        self.assertIsInstance(result, approved_renames.OuterBindingUnproved)
        assert isinstance(result, approved_renames.OuterBindingUnproved)
        self.assertIn("item path is not unique", result.reason)

    def test_current_admission_reverses_proven_format_capture_edits(self) -> None:
        source = CAPTURE_BINDING_ITEM
        rename = BindingRename(2, source.splitlines()[1], "g", "checkout_info")
        edited = source.replace("g =", "checkout_info =").replace("{g}", "{checkout_info}")
        proof = binding_rename_check("src/lib.rs", source, edited, (rename,))
        self.assertEqual(proof.checks[rename], AppliedInCode())
        self.assertTrue(proof.explained)
        admitted = approved_renames.binding_admission_check(source, (rename,), proof, current=edited)
        self.assertEqual(admitted.checks[rename], AppliedInCode())
        self.assertTrue(admitted.explained)

    def test_current_admission_retains_a_later_outer_binding_after_undoing_just_the_rename(self) -> None:
        before = "fn label() {\nmap(|g| g + 1)\n}\n"
        rename = BindingRename(2, before.splitlines()[1], "g", "checkout_info")
        after = before.replace("label()", "label(checkout_info: usize)").replace("|g| g", "|checkout_info| checkout_info")
        proof = approved_renames.binding_admission_check(before, (rename,), binding_rename_check(
            "src/lib.rs", before, after, (rename,)), current=after)
        result = proof.checks[rename]
        self.assertIsInstance(result, approved_renames.OuterBindingBlocked)
        assert isinstance(result, approved_renames.OuterBindingBlocked)
        self.assertEqual((result.name, result.line), ("checkout_info", 1))

    def test_every_enclosing_brace_scope_contributes_its_inner_attributes(self) -> None:
        for label, source in INHERITED_INNER_ATTRIBUTES:
            with self.subTest(scope=label):
                rename = BindingRename(2, source.splitlines()[1], "g", "checkout_info")
                result = approved_renames.rename_admission(source, rename)
                self.assertIsInstance(result, approved_renames.OuterBindingUnproved)
                assert isinstance(result, approved_renames.OuterBindingUnproved)
                self.assertIn("unresolved attribute macro 'inject'", result.reason)
                edited = source.replace("|g| g", "|checkout_info| checkout_info")
                proof = approved_renames.binding_admission_check(source, (rename,), binding_rename_check(
                    "src/lib.rs", source, edited, (rename,)), current=edited)
                self.assertEqual(proof.checks[rename], result)

    def test_stale_local_sites_still_require_admission_but_nonlocal_targets_keep_their_proof(self) -> None:
        source = "fn label(checkout_info: usize) {\n    map(|g| g + 1)\n}\n"
        rename = BindingRename(2, "map(|g| g + 2)", "g", "checkout_info")
        expected = approved_renames.OuterBindingBlocked(
            "rename blocked by outer binding 'checkout_info' at line 1", "checkout_info", 1,
            source.splitlines()[0])
        proof = binding_rename_check("src/lib.rs", source, source, (rename,))
        self.assertEqual(approved_renames.binding_admission_check(source, (rename,), proof, current=source).checks[rename], expected)
        for nonlocal_source, pair in (
                ("fn old() {}\n", BindingRename(1, "fn old() { changed(); }", "old", "fresh")),
                ("struct Value { old: usize }\n", BindingRename(1, "struct Value { old: u8 }", "old", "fresh")),
                ("enum Outcome { NewName }\n", BindingRename(2, "rename", "helper", "assistant"))):
            with self.subTest(nonlocal_source=nonlocal_source):
                existing = binding_rename_check("src/lib.rs", nonlocal_source, nonlocal_source, (pair,))
                self.assertEqual(approved_renames.binding_admission_check(
                    nonlocal_source, (pair,), existing, current=nonlocal_source).checks, existing.checks)
        missing = BindingRename(2, "map(|gone| gone + 1)", "gone", "fresh")
        self.assertIsInstance(approved_renames.rename_admission(source, missing),
                              approved_renames.OuterBindingUnproved)
        self.assertIsInstance(approved_renames.rename_admission(
            "inject!();", BindingRename(1, "rename", "g", "checkout_info")),
                              approved_renames.OuterBindingUnproved)

    def test_every_local_binding_form_keeps_admission_when_its_site_text_is_stale(self) -> None:
        for body in (
                "map(|g| g + 1);", "let g = input; consume(g);",
                "let (g, _) = input; consume(g);", "match input { Some(g) => consume(g), _ => () }",
                "for g in input { consume(g); }", "if let Some(g) = input { consume(g); }",
                "while let Some(g) = next() { consume(g); }"):
            with self.subTest(body=body):
                source = "fn label(checkout_info: usize) {\n" + body + "\n}\n"
                pair = BindingRename(2, body + " // stale", "g", "checkout_info")
                proof = binding_rename_check("src/lib.rs", source, source, (pair,))
                self.assertIsInstance(approved_renames.binding_admission_check(source, (pair,), proof, current=source).checks[pair],
                                      approved_renames.OuterBindingBlocked | approved_renames.OuterBindingUnproved)
        source = "fn label(checkout_info: usize, g: usize) { consume(g); }\n"
        pair = BindingRename(1, source.strip() + " // stale", "g", "checkout_info")
        self.assertIsInstance(approved_renames.binding_admission_check(source, (pair,), binding_rename_check(
            "src/lib.rs", source, source, (pair,)), current=source).checks[pair],
                              approved_renames.OuterBindingBlocked | approved_renames.OuterBindingUnproved)

    def test_inner_attributes_of_the_file_and_enclosing_modules_require_admission(self) -> None:
        for source in (
                "mod outer { #![inject] fn label() {\nmap(|g| g + 1)\n} }\n",
                "#![inject]\nfn label() { map(|g| g + 1) }\n",
                "mod outer { #![allow(dead_code)] mod inner { #![inject] fn label() {\nmap(|g| g + 1)\n} } }\n"):
            with self.subTest(source=source):
                rename = BindingRename(2, source.splitlines()[1], "g", "checkout_info")
                result = approved_renames.rename_admission(source, rename)
                self.assertIsInstance(result, approved_renames.OuterBindingUnproved)
                assert isinstance(result, approved_renames.OuterBindingUnproved)
                self.assertIn("unresolved attribute macro 'inject'", result.reason)
                proof = approved_renames.binding_admission_check(source, (rename,), binding_rename_check(
                    "src/lib.rs", source, source.replace("|g| g", "|checkout_info| checkout_info"), (rename,)), current=source)
                self.assertEqual(proof.checks[rename], result)
        for source in (
                "#![allow(dead_code)]\nfn label() { map(|g| g + 1) }\n",
                "mod sibling { #![inject] fn earlier() {} }\nfn label() { map(|g| g + 1) }\n"):
            with self.subTest(unrelated_or_inert=source):
                self.assertIsInstance(approved_renames.rename_admission(
                    source, BindingRename(2, source.splitlines()[1], "g", "checkout_info")), RenameDeclaration)

    def test_item_presence_policy_handles_astra_cases_and_unread_targets(self) -> None:
        for label, source, line, expected in PRESENCE_REFUSALS:
            rename = BindingRename(line, source.splitlines()[line - 1], "g", "checkout_info")
            with self.subTest(form=label):
                result = approved_renames.rename_admission(source, rename)
                self.assertIsInstance(result, expected)
                self.assertEqual(approved_renames.binding_admission_check(source, (rename,), binding_rename_check("src/lib.rs", source, source, (rename,)), current=source).checks[rename], result)

    def test_enclosing_scope_closed_grammar_corpus(self) -> None:
        unproved = approved_renames.OuterBindingUnproved
        blocked = approved_renames.OuterBindingBlocked
        rows = (
            ("less-than let before unread condition", "let flag = left < right;\n"
             + "if let Some(checkout_info) = if flag { value } else { other } {\nTARGET\n}", unproved),
            ("less-than let before macro", "let flag = left < right; introduce!(checkout_info);\nTARGET", unproved),
            ("greater-than let", "let flag = left > right;\nTARGET", RenameDeclaration),
            ("less-than condition", "if left < right {\nTARGET\n}", RenameDeclaration),
            ("greater-than condition", "if left > right {\nTARGET\n}", RenameDeclaration),
            ("less-than argument", "consume(left < right);\nTARGET", RenameDeclaration),
            ("greater-than argument", "consume(left > right);\nTARGET", RenameDeclaration),
            ("less-than preceding argument", "consume(left < right,\nTARGET\n);", RenameDeclaration),
            ("shift", "let flag = left << right;\nTARGET", RenameDeclaration),
            ("turbofish", "let value = value::<usize>();\nTARGET", RenameDeclaration),
            ("const generic", "let value = value::<{ 1 }>();\nTARGET", RenameDeclaration),
            ("generic annotation", "let value: Vec<Vec<usize>> = Vec::new();\nTARGET", RenameDeclaration),
            ("if-let else", "if let Some(checkout_info) = value::<{ 1 }>() { consume(checkout_info); }"
             + " else {\nTARGET\n}", unproved),
            ("while-let then", "while let Some(checkout_info) = value {\nTARGET\n}", blocked),
            ("completed while-let", "while let Some(checkout_info) = value { consume(checkout_info); }\nTARGET",
             unproved),
            ("match arm", "match value { Some(checkout_info) => {\nTARGET\n}, _ => {} }", blocked),
            ("sibling match arm", "match value { Some(checkout_info) => {}, _ => {\nTARGET\n} }", unproved),
            ("nested closure", "consume(|checkout_info| {\nTARGET\n});", blocked),
            ("preceding macro", "introduce!(checkout_info);\nTARGET", unproved),
            ("sibling macro", "{ introduce!(checkout_info); }\nTARGET", unproved),
        )
        for label, body, expected in rows:
            source = "fn label() {\n" + body.replace("TARGET", "map(|g| g + 1)") + "\n}\n"
            line = next(i for i, text in enumerate(source.splitlines(), 1) if "|g|" in text)
            rename = BindingRename(line, source.splitlines()[line - 1], "g", "checkout_info")
            with self.subTest(form=label):
                self.assertIsInstance(approved_renames.rename_admission(source, rename), expected)

    def test_preceding_enclosing_macro_cannot_hide_a_binding(self) -> None:
        source = "fn label() { introduce!(checkout_info);\n    map(|g| g + 1)\n}\n"
        rename = BindingRename(2, source.splitlines()[1], "g", "checkout_info")
        result = approved_renames.rename_admission(source, rename)
        self.assertIsInstance(result, approved_renames.OuterBindingUnproved)
        assert isinstance(result, approved_renames.OuterBindingUnproved)
        self.assertEqual(result.reason, "unresolved bang macro 'introduce' at line 1")
        self.assertEqual(approved_renames.binding_admission_check(source, (rename,), binding_rename_check("src/lib.rs", source, source, (rename,)), current=source).checks[rename], result)

    def test_unread_sibling_scopes_cannot_reject_a_later_unblocked_binding(self) -> None:
        for earlier in (
                "fn earlier() { if let Some(checkout_info) = value::<{ 1 }>() { consume(checkout_info); } }\n",
                "fn earlier() { introduce!(checkout_info); }\n",
                "fn earlier(checkout_info: usize) {\n    fn label() {\n        map(|g| g + 1)\n    }\n}\n"):
            source = earlier if "|g|" in earlier else earlier + "fn label() {\n    map(|g| g + 1)\n}\n"
            line = next(i for i, text in enumerate(source.splitlines(), 1) if "|g|" in text)
            rename = BindingRename(line, source.splitlines()[line - 1], "g", "checkout_info")
            with self.subTest(source=source):
                self.assertIsInstance(approved_renames.rename_admission(source, rename), RenameDeclaration)
        # Even within one function, a completed sibling condition cannot enclose the target.
        source = ("fn label() {\n    if let Some(checkout_info) = value::<{ 1 }>() { consume(checkout_info); }\n"
                  "    map(|g| g + 1)\n}\n")
        self.assertIsInstance(approved_renames.rename_admission(source, BindingRename(3, source.splitlines()[2], "g", "checkout_info")),
                              approved_renames.OuterBindingUnproved)
        for source in (
                "fn label() { { introduce!(checkout_info); }\n    map(|g| g + 1)\n}\n",
                "fn label() { match value {\n    First => introduce!(checkout_info),\n"
                + "    Second => map(|g| g + 1),\n} }\n",
                "fn label() { println!(\"earlier\");\n    map(|g| g + 1)\n}\n"):
            line = next(i for i, text in enumerate(source.splitlines(), 1) if "|g|" in text)
            with self.subTest(source=source):
                self.assertIsInstance(approved_renames.rename_admission(source, BindingRename(line, source.splitlines()[line - 1],
                                                                               "g", "checkout_info")),
                                      RenameDeclaration if "println!" in source else approved_renames.OuterBindingUnproved)

    def test_same_line_outer_declarations_keep_their_exact_binding_identity(self) -> None:
        source = "fn label(checkout_info: usize) { let checkout_info = 1;\n    map(|g| g + 1)\n}\n"
        rename = BindingRename(2, source.splitlines()[1], "g", "checkout_info")
        result = approved_renames.rename_admission(source, rename)
        self.assertIsInstance(result, approved_renames.OuterBindingBlocked)
        assert isinstance(result, approved_renames.OuterBindingBlocked)
        self.assertEqual((result.name, result.line, result.text), ("checkout_info", 1, source.splitlines()[0]))
        self.assertEqual(approved_renames.binding_admission_check(source, (rename,), binding_rename_check("src/lib.rs", source, source, (rename,)), current=source).checks[rename], result)

    def test_raw_outer_binding_and_plain_destination_are_the_same_identifier(self) -> None:
        source = "fn label(r#checkout_info: usize) {\n    map(|g| g + 1)\n}\n"
        rename = BindingRename(2, source.splitlines()[1], "g", "checkout_info")
        result = approved_renames.rename_admission(source, rename)
        self.assertIsInstance(result, approved_renames.OuterBindingBlocked)
        assert isinstance(result, approved_renames.OuterBindingBlocked)
        self.assertEqual((result.name, result.line), ("checkout_info", 1))
        self.assertEqual(approved_renames.binding_admission_check(source, (rename,), binding_rename_check("src/lib.rs", source, source, (rename,)), current=source).checks[rename], result)
        # Normalization must also subtract a raw local shadow's scope from its plain enclosing parameter.
        source = "fn label(checkout_info: usize) { let r#checkout_info = 1;\n    map(|g| g + 1)\n}\n"
        self.assertIsInstance(approved_renames.rename_admission(source, rename), approved_renames.OuterBindingBlocked)

    def test_undecidable_outer_visibility_is_an_explicit_unproved_result(self) -> None:
        source = "fn label() {\n    if let Some(checkout_info) = value::<{ 1 }>() {\n        map(|g| g + 1);\n    }\n}\n"
        rename = BindingRename(3, source.splitlines()[2], "g", "checkout_info")
        result = approved_renames.rename_admission(source, rename)
        self.assertIsInstance(result, NoBindingScope)
        self.assertIsInstance(result, approved_renames.OuterBindingUnproved)
        assert isinstance(result, approved_renames.OuterBindingUnproved)
        self.assertEqual(result.reason, "wanted name already used in this item: 'checkout_info' at line 2")
        self.assertEqual(approved_renames.binding_admission_check(source, (rename,), binding_rename_check("src/lib.rs", source, source, (rename,)), current=source).checks[rename], result)

    def test_closure_rename_reports_the_visible_outer_binding_in_the_shared_proof(self) -> None:
        source = "fn label(checkout_info: usize) -> usize {\n    map(|g| g + 1)\n}\n"
        pair = BindingRename(2, source.splitlines()[1], "g", "checkout_info")
        blocked = approved_renames.rename_admission(source, pair)
        self.assertIsInstance(blocked, approved_renames.OuterBindingBlocked)
        assert isinstance(blocked, approved_renames.OuterBindingBlocked)
        self.assertEqual((blocked.name, blocked.line, blocked.text),
                         ("checkout_info", 1, source.splitlines()[0]))
        self.assertEqual(approved_renames.binding_admission_check(source, (pair,), binding_rename_check("src/lib.rs", source, source, (pair,)), current=source).checks[pair], blocked)
        unblocked = BindingRename(2, pair.text, "g", "entry")
        edited = source.replace("|g| g", "|entry| entry")
        self.assertEqual(approved_renames.binding_admission_check(source, (unblocked,), binding_rename_check("src/lib.rs", source, edited, (unblocked,)), current=source).checks[unblocked],
                         AppliedInCode())

        source = "fn outer(checkout_info: usize) {\n    fn inner(g: usize) -> usize { g }\n}\n"
        pair = BindingRename(2, source.splitlines()[1], "g", "checkout_info")
        self.assertIsInstance(approved_renames.rename_admission(source, pair), RenameDeclaration)

    def test_an_enum_variant_rename_is_applied_only_when_the_old_variant_is_gone(self) -> None:
        self.assertEqual(check("Kind::Old", "Kind::New", "src/kind.rs", "pub enum Kind { New, Other }\n"), AppliedInCode())
        self.assertEqual(check("Kind::Old", "Kind::New", "src/kind.rs", "pub enum Kind { Old, Other }\n"),
                         RenameNotApplied("src/kind.rs"))

    def test_a_struct_field_rename_is_applied_only_when_the_old_field_is_gone(self) -> None:
        applied = "pub struct Config {\n    #[serde(default)]\n    pub(crate) new_field: u8,\n    map: HashMap<u8, std::string::String>,\n}\n"
        self.assertEqual(check("Config::old_field", "Config::new_field", "src/config.rs", applied), AppliedInCode())
        self.assertEqual(check("Config::old_field", "Config::new_field", "src/config.rs",
                               applied.replace("new_field", "old_field")), RenameNotApplied("src/config.rs"))

    def test_a_top_level_item_of_a_file_module_is_under_the_module(self) -> None:
        source = "pub fn new_fn() {}\nmod tests { fn reads() { super::new_fn(); } }\n"
        self.assertEqual(check("paths::old_fn", "paths::new_fn", "src/paths.rs", source), AppliedInCode())
        self.assertEqual(check("paths::old_fn", "paths::new_fn", "src/paths/mod.rs", source), AppliedInCode())
        self.assertEqual(check("paths::old_fn", "paths::new_fn", "src/paths.rs", "pub fn old_fn() {}\n"),
                         RenameNotApplied("src/paths.rs"))

    def test_a_top_level_item_of_a_crate_root_is_under_crate(self) -> None:
        self.assertEqual(check("crate::OldType", "crate::NewType", "src/lib.rs", "pub struct NewType;\nmod inner;\n"),
                         AppliedInCode())
        self.assertEqual(check("crate::OldType", "crate::NewType", "src/main.rs", "pub struct OldType;\n"),
                         RenameNotApplied("src/main.rs"))

    def test_a_file_that_declares_neither_name_under_the_parent_has_no_check(self) -> None:
        use_site = "use crate::kind::Kind;\nfn read() -> Kind { Kind::New }\n"
        self.assertEqual(check("Kind::Old", "Kind::New", "src/read.rs", use_site), NoAppliedCheck("rename"))
        self.assertEqual(check("crate::OldType", "crate::NewType", "src/types.rs", "pub struct NewType;\n"),
                         NoAppliedCheck("rename"))

    def test_a_file_that_holds_both_names_has_no_check(self) -> None:
        both = (("paths::old_fn", "paths::new_fn", "src/paths.rs", "pub fn new_fn() {}\nmod tests { fn old_fn() {} }\n"),
                ("Parser::next", "Parser::advance", "src/parser.rs",
                 "pub struct Parser;\nimpl Parser { fn advance() {} }\nimpl Iterator for Parser { fn next() {} }\n"),
                ("Config::enabled", "Config::is_enabled", "src/config.rs",
                 "pub struct Config { is_enabled: bool }\nimpl Config { pub fn enabled(&self) -> bool { self.is_enabled } }\n"),
                ("ffi::old", "ffi::new", "src/ffi.rs", 'pub fn new() {}\nextern "C" { fn old(); }\n'),
                ("ffi::old", "ffi::new", "src/lib.rs", "mod ffi { pub fn new() {} items! { fn old() {} } }\n"),
                ("Kind::Old", "Kind::New", "src/kind.rs", "struct Kind;\nimpl Kind { fn New() {} }\n"
                 + "mod other { struct Kind; impl Kind { fn Old() {} } }\n"))
        for before, after, path, source in both:
            self.assertEqual(check(before, after, path, source), NoAppliedCheck("rename"), source)

    def test_a_parent_name_that_more_than_one_item_has_gives_no_check(self) -> None:
        source = "mod a { enum Kind { Gone } }\nmod b { enum Kind { New } }\n"
        self.assertEqual(check("a::Kind::Old", "a::Kind::New", "src/lib.rs", source), NoAppliedCheck("rename"))

    def test_only_inherent_impls_declare_names_under_a_type(self) -> None:
        trait_impl = "pub struct Parser;\nimpl Iterator for Parser { fn next() {} }\n"
        self.assertEqual(check("Parser::next", "Parser::advance", "src/parser.rs", trait_impl), NoAppliedCheck("rename"))
        self.assertEqual(check("Parser::next", "Parser::advance", "src/parser.rs", trait_impl.replace("Iterator for ", "")),
                         RenameNotApplied("src/parser.rs"))

    def test_the_impls_of_a_trait_declare_names_under_the_trait(self) -> None:
        source = "pub trait Shape { fn sides(); }\nimpl<T> Shape for Square<T> { fn area() {} }\n"
        self.assertEqual(check("Shape::size", "Shape::area", "src/shape.rs", source), AppliedInCode())
        self.assertEqual(check("Shape::size", "Shape::area", "src/shape.rs", source.replace("area", "size")),
                         RenameNotApplied("src/shape.rs"))

    def test_impls_that_name_the_parent_by_different_paths_give_no_check(self) -> None:
        source = "impl a::Kind { fn new_fn() {} }\nimpl b::Kind { fn other() {} }\n"
        self.assertEqual(check("a::Kind::old_fn", "a::Kind::new_fn", "src/lib.rs", source), NoAppliedCheck("rename"))
        self.assertEqual(check("a::Kind::old_fn", "a::Kind::new_fn", "src/lib.rs", source.replace("b::Kind", "a::Kind")),
                         AppliedInCode())

    def test_a_field_and_an_item_of_one_old_name_without_the_new_name_are_not_applied(self) -> None:
        accessor = "struct Config { name: String }\nimpl Config { fn name(&self) -> &str { &self.name } }\n"
        self.assertEqual(check("Config::name", "Config::label", "src/config.rs", accessor), RenameNotApplied("src/config.rs"))

    def test_a_higher_ranked_bound_does_not_make_an_impl_a_trait_impl(self) -> None:
        bound = "impl<F> Parser<F> where F: for<'a> Fn(&'a u8) { fn old(&self) {} }\n"
        self.assertEqual(check("Parser::old", "Parser::new", "src/parser.rs", bound), RenameNotApplied("src/parser.rs"))
        boxed = "impl Parser<Box<dyn for<'a> Fn(&'a u8)>> { fn old() {} }\n"
        self.assertEqual(check("Parser::old", "Parser::new", "src/parser.rs", boxed), RenameNotApplied("src/parser.rs"))

    def test_an_impl_under_another_module_gives_no_check(self) -> None:
        source = "mod a { pub enum Kind { Gone } }\nimpl crate::b::Kind { pub const New: u8 = 0; }\n"
        self.assertEqual(check("a::Kind::Old", "a::Kind::New", "src/lib.rs", source), NoAppliedCheck("rename"))
        local = "mod a { pub enum Kind { Gone } impl Kind { pub const Old: u8 = 0; } }\n"
        self.assertEqual(check("a::Kind::Old", "a::Kind::New", "src/lib.rs", local), RenameNotApplied("src/lib.rs"))

    def test_an_impl_of_a_parent_by_another_path_or_in_another_module_declares_nothing(self) -> None:
        writer = ("pub trait Write { fn sync(&self); }\n"
                  + "impl io::Write for Buf { fn write(&mut self) {} fn flush(&mut self) {} }\n")
        self.assertEqual(check("Write::flush", "Write::sync", "src/write.rs", writer), NoAppliedCheck("rename"))
        self.assertEqual(sites("Write::flush", "Write::sync", "src/write.rs", writer, "flush"), UncertainScan())
        imported = ("pub struct Kind;\nimpl Kind { pub fn new() {} }\n"
                    + "mod other { use crate::legacy::Kind; impl Kind { pub fn old() {} } }\n")
        self.assertEqual(check("Kind::old", "Kind::new", "src/kind.rs", imported), NoAppliedCheck("rename"))
        self.assertEqual(sites("Kind::old", "Kind::new", "src/kind.rs", imported, "old"), UncertainScan())

    def test_an_impl_of_the_parent_inside_a_block_that_is_not_a_home_module_gives_an_uncertain_scan(self) -> None:
        blocks = ("fn other() { use crate::legacy::Kind; impl Kind { pub fn old() {} } }",
                  "const _: () = { use crate::legacy::Kind; impl Kind { pub fn old() {} } };",
                  "macro_rules! m { () => { impl Kind { pub fn old() {} } } }")
        for block in blocks:
            source = f"pub struct Kind;\nimpl Kind {{ pub fn old() {{}} }}\n{block}\n"
            self.assertEqual(sites("Kind::old", "Kind::new", "src/kind.rs", source, "old"), UncertainScan(), source)

    def test_a_macro_or_attribute_that_can_make_names_gives_no_check(self) -> None:
        sources = ("pub struct Config { width: u32 }\n"
                   + "impl Config { paste::paste! { pub fn [<get_ width>](&self) -> u32 { self.width } } }\n",
                   '#[derive(Getters)]\n#[getset(get = "pub with_prefix")]\npub struct Config { width: u32 }\n',
                   'pub struct Config { width: u32 }\nimpl Config { include!("legacy.rs"); }\n')
        for source in sources:
            self.assertEqual(check("Config::get_width", "Config::width", "src/config.rs", source),
                             NoAppliedCheck("rename"), source)
        derived = "#[derive(Clone, Debug)]\npub struct Config { width: u32 }\n"
        self.assertEqual(check("Config::get_width", "Config::width", "src/config.rs", derived), AppliedInCode())

    def test_an_item_keyword_counts_only_at_an_item_start(self) -> None:
        grid = "pub struct Grid;\nimpl Grid { pub const SIZE: usize = 4; pub fn fill<const N: usize>(&self) {} }\n"
        self.assertEqual(sites("Grid::N", "Grid::SIZE", "src/grid.rs", grid, "N"), Declarations(frozenset()))
        self.assertEqual(sites("Grid::N", "Grid::SIZE", "src/grid.rs", grid, "SIZE"), Declarations(frozenset({9})))
        config = "use std::io::Error;\npub enum ConfigError { Io }\npub fn last() -> &'static Error { todo!() }\n"
        self.assertEqual(sites("config::Error", "config::ConfigError", "src/config.rs", config, "Error"),
                         Declarations(frozenset()))

    def test_a_file_that_holds_the_old_name_in_any_form_has_no_check(self) -> None:
        forms = ("pub use self::new_fn as old_fn;", "pub use crate::legacy::old_fn;", "extern crate old_fn;",
                 "macro_rules! old_fn { () => {} }")
        for form in forms:
            source = f"pub fn new_fn() {{}}\n{form}\n"
            self.assertEqual(check("paths::old_fn", "paths::new_fn", "src/paths.rs", source), NoAppliedCheck("rename"),
                             source)

    def test_a_form_the_scan_does_not_read_gives_no_check(self) -> None:
        cases = (("Config::old", "Config::new", "pub struct Config { new: u8, r#old: u8 }\n"),
                 ("Parser::old", "Parser::new",
                  "pub struct Parser;\ntype P = Parser;\nimpl Parser { fn new() {} }\nimpl P { fn old() {} }\n"),
                 ("Shape::old", "Shape::new",
                  "pub trait Shape { fn new(&self); }\nimpl dyn Shape { pub fn old(&self) {} }\n"),
                 ("Config::old", "Config::new",
                  "macro_rules! cfg { ($f:ident) => { pub struct Config { new: u8, $f: u8 } } }\ncfg!(old);\n"))
        for before, after, source in cases:
            self.assertEqual(check(before, after, "src/lib.rs", source), NoAppliedCheck("rename"), source)
        other_module = "mod a;\nmod b { pub enum Kind { New } }\n"
        self.assertEqual(check("a::Kind::Old", "a::Kind::New", "src/lib.rs", other_module), NoAppliedCheck("rename"))
        self.assertEqual(check("b::Kind::Old", "b::Kind::New", "src/lib.rs", other_module), AppliedInCode())

    def test_a_finding_that_is_not_a_qualified_rename_has_no_check(self) -> None:
        self.assertEqual(applied_check("type", "u32", "u64", "src/lib.rs", files({"src/lib.rs": "fn work() -> u64 { 1 }\n"})),
                         NoAppliedCheck("type"))
        self.assertEqual(check("old_name", "new_name", "src/lib.rs", "fn new_name() {}\n"), NoAppliedCheck("rename"))


class FileRenameTests(unittest.TestCase):
    def test_a_file_rename_is_read_from_the_finding_path_and_the_paths_its_after_names(self) -> None:
        path = "crates/cargo-berth/src/ledger/lock.rs"
        accepted = (
            ("Rename ledger/lock.rs to ledger/mutation_lock.rs; update ledger/mod.rs and all super::lock paths",
             "crates/cargo-berth/src/ledger/mutation_lock.rs"),
            ("Rename ledger/lock.rs to ledger/mutation_lock.rs (the type it holds).",
             "crates/cargo-berth/src/ledger/mutation_lock.rs"),
            ("Rename src/ledger/lock.rs to src/ledger/guard.rs.", "crates/cargo-berth/src/ledger/guard.rs"),
            ("Rename lock.rs to guard.rs", "crates/cargo-berth/src/ledger/guard.rs"),
            ("Rename ledger/lock.rs to ledger/mutation_lock.rs, update the mod and pub use",
             "crates/cargo-berth/src/ledger/mutation_lock.rs"),
        )
        for after, new in accepted:
            with self.subTest(after=after):
                self.assertEqual(file_rename("rename", path, after), FileRename(path, new))
        refused = (
            ("rename", "Rename ledger/lock.rs to ledger/mutation_lock.rs", "src/other/lock.rs"),
            ("rename", "Rename ledger/lock.rs to ledger/mutation_lock.rs", "src/my_ledger/lock.rs"),
            ("type", "Rename ledger/lock.rs to ledger/mutation_lock.rs", path),
            ("rename", "Move ledger/lock.rs to ledger/mutation_lock.rs", path),
            ("rename", "Rename ledger/lock.rs to store/lock.rs", path),
            ("rename", "Rename ledger/lock.rs to ledger/lock.rs", path),
            ("rename", "Rename ledger/lock.rs to ledger/mod.rs", path),
            ("rename", "Rename ledger/lock.rs to ledger/mutation_lock.rs.bak", path),
            ("rename", "Rename ledger/lock.rs to ledger/1lock.rs", path),
            ("rename", "Rename lock.rs to ../lock_guard.rs", path),
            ("rename", "MutationLock::lock", path),
            ("rename", "Rename ledger/lock.rs to ledger/mutation_lock.rs and move LockError into ledger/error.rs", path),
            ("rename", "Rename ledger/lock.rs to ledger/mutation_lock.rs, ledger/guard.rs to ledger/mutation_guard.rs",
             path),
            ("rename", "Rename ledger/lock.rs to ledger/type.rs", path),
            ("rename", "Rename ledger/type.rs to ledger/kind.rs", "crates/cargo-berth/src/ledger/type.rs"),
            ("rename", "Rename ledger/lock.rs to ledger/mutation_lock.rs; then ledger/mod.rs to ledger.rs", path),
        )
        for kind, after, finding_path in refused:
            with self.subTest(after=after, path=finding_path):
                self.assertEqual(file_rename(kind, finding_path, after), NoFileRename())

    def test_a_negated_move_after_a_file_rename_is_not_another_change(self) -> None:
        path = "crates/cargo-berth/src/ledger/lock.rs"
        rename = "Rename ledger/lock.rs to ledger/mutation_lock.rs; "
        negated = (
            "keep the dispatch and inline tests together; no existing definition moves from those callers.",
            "update the imports; no types or behavior move into skipped files.",
            "No existing definition moves into or out of a skipped file; callers update their paths.",
            "the dispatch doesn't move.",
            "this file cannot move.",
            "keep the API, and no facade or additional split is introduced.",
            "keep load_report and the finding-merging helpers together.",
            "the dispatch does not move.",
            "keep the tests here without moving them.",
            "create no extra facade and move no skipped definitions.",
        )
        for rest in negated:
            with self.subTest(rest=rest):
                self.assertEqual(file_rename("rename", path, rename + rest),
                                 FileRename(path, "crates/cargo-berth/src/ledger/mutation_lock.rs"))
        moves = (
            "move scroll_indicator to its title.rs sole caller.",
            "apply this to the residue after generic progress moves to tui_pane.",
            "line_offsets and offset move to imports/scan.rs.",
            "keep no facade, and move LockError into ledger/error.rs.",
            "no facade stays but split the guard into its own file.",
            "Merge LockError into ledger/error.rs.",
            "without delay move LockError into ledger/error.rs.",
            "move no fewer than two definitions into ledger/error.rs.",
            "rename ledger/error.rs TO ledger/failure.rs.",
            "with no further delay move LockError into ledger/error.rs.",
            "not only move LockError into ledger/error.rs.",
            "move nothing but LockError into ledger/error.rs.",
            "No doubt move LockError into ledger/error.rs.",
            "Do not introduce a facade or move any sibling definition.",
            "move nothing apart from LockError into ledger/error.rs.",
            "No longer needed compatibility helpers move into ledger/legacy.rs.",
            "no doubt LockError moves into ledger/error.rs.",
            "No fewer than two helpers move into ledger/legacy.rs.",
            "move no helpers, apart from LockError, into ledger/error.rs.",
            "no helpers move, apart from LockError.",
            "move no other helpers than LockError into ledger/error.rs.",
            "move no-op helpers into ledger/legacy.rs.",
            "it moves no item into an existing skipped file.",
            "move no other helpers into ledger/error.rs except LockError.",
        )
        for rest in moves:
            with self.subTest(rest=rest):
                self.assertEqual(file_rename("rename", path, rename + rest), NoFileRename())

    def test_a_file_rename_with_a_folder_of_child_modules_has_no_check(self) -> None:
        old, new = "src/ledger/lock.rs", "src/ledger/mutation_lock.rs"
        after = "Rename ledger/lock.rs to ledger/mutation_lock.rs"
        self.assertEqual(applied_check("rename", "", after, old, files({new: "mod guard;\n"})), AppliedInCode())
        for folder in ("src/ledger/lock/guard.rs", "src/ledger/mutation_lock/guard.rs"):
            with self.subTest(folder=folder):
                self.assertEqual(applied_check("rename", "", after, old, files({new: "mod guard;\n", folder: ""})),
                                 NoAppliedCheck("rename"))


class BindingRenameTests(unittest.TestCase):
    def test_resolution_candidate_does_not_relax_the_ledger_proof(self) -> None:
        for header, cause in (("type old = usize;", "old name also occurs as an item or type"),
                              ("fn g() { let fresh = 2; }", "new name already occurs in file")):
            source = header + "\nfn f() {\n let old = 1;\n let _ = old + 1;\n}\n"
            rename = BindingRename(3, source.splitlines()[2], "old", "fresh")
            self.assertEqual(binding_scope(source, rename), NoBindingScope(cause))
            self.assertIsInstance(resolution_scope(source, rename), BindingScope)
            proof = binding_rename_check("src/lib.rs", source, source.replace("let old", "let fresh"), (rename,))
            self.assertEqual(proof.refusals[rename], cause)
            self.assertFalse(proof.explained)

    def test_resolution_candidate_keeps_a_glob_hidden_by_the_new_name_refusal(self) -> None:
        source = "use provider::*; fn g() { let fresh = 2; }\nfn f() {\n let old = 1;\n}\n"
        rename = BindingRename(3, source.splitlines()[2], "old", "fresh")
        self.assertEqual(resolution_scope(source, rename), NoBindingScope("file contains a glob import"))

    def prove(self, source: str, edited: str, line: int = 2, old: str = "old", new: str = "fresh") -> BindingProof:
        rename = BindingRename(line, source.splitlines()[line - 1], old, new)
        return binding_rename_check("src/lib.rs", source, edited, (rename,))

    def accepted(self, source: str, edited: str, kind: str, line: int = 2) -> None:
        rename = BindingRename(line, source.splitlines()[line - 1], "old", "fresh")
        scope = binding_scope(source, rename)
        self.assertIsInstance(scope, BindingScope)
        assert isinstance(scope, BindingScope)
        self.assertEqual(scope.kind, kind)
        proof = self.prove(source, edited, line)
        self.assertEqual(proof.checks, {rename: AppliedInCode()})
        self.assertEqual(proof.refusals, {})
        self.assertTrue(proof.explained)

    def test_reads_bare_and_snippet_pairs_but_refuses_qualified_raw_keyword_and_multiple_pairs(self) -> None:
        for before, after in (("old", "fresh"), ("    old: &Thing,", "    fresh: &Thing,"),
                              ("(old, old)", "(fresh, fresh)")):
            self.assertEqual(binding_rename("rename", before, after, 7, "line"), BindingRename(7, "line", "old", "fresh"))
        for kind, before, after in (("type", "old", "fresh"), ("rename", "Parent::old", "Parent::fresh"),
                                    ("rename", "r#old", "fresh"), ("rename", "old", "let"),
                                    ("rename", "(old, a)", "(fresh, b)"), ("rename", "old", "old + fresh")):
            self.assertEqual(binding_rename(kind, before, after, 7, "line"), NoBindingRename())

    def test_statement_let_and_reflow_line_map(self) -> None:
        source = "fn f() {\n    let old = 1;\n    consume(old);\n}\n"
        edited = "fn f() {\n\n    let fresh =\n        1;\n    consume(fresh);\n}\n"
        self.accepted(source, edited, "let")
        self.assertEqual(self.prove(source, edited).line_map, {1: 1, 2: 3, 3: 5, 4: 6})

    def test_condition_let_scopes_cover_chains_and_body_but_not_else_or_scrutinee(self) -> None:
        for keyword in ("if", "while"):
            source = (f"fn f() {{\n    {keyword} let Some(old) = old && old.ready() {{\n" +
                      "        consume(old);\n    }" + (" else { consume(old); }" if keyword == "if" else "\n    consume(old);")
                      + "\n}\n")
            edited = source.replace("Some(old)", "Some(fresh)").replace("&& old", "&& fresh").replace(
                "        consume(old)", "        consume(fresh)")
            self.accepted(source, edited, "condition let")
        source = "fn f() {\n    if ready && let Some(old) = input { consume(old); }\n}\n"
        self.accepted(source, source.replace("old", "fresh"), "condition let")

    def test_match_arm_guard_and_body_stay_separate_from_other_arms(self) -> None:
        source = ("fn f() { match input {\n    Some(old) if old.ready() => { consume(old); }\n"
                  "    None => consume(old),\n} }\n")
        self.accepted(source, source.replace("Some(old) if old.ready() => { consume(old)",
                                            "Some(fresh) if fresh.ready() => { consume(fresh)"), "match arm")
        source = "fn f() { match input {\n    Some(old) => consume(old),\n    None => consume(old),\n} }\n"
        self.accepted(source, source.replace("Some(old) => consume(old)", "Some(fresh) => consume(fresh)"), "match arm")

    def test_fn_parameters_include_generics_and_nested_pattern_binding_but_not_field_label(self) -> None:
        for parameter in ("old: &T", "Foo { label: old }: Foo", "(old, other): (T, T)"):
            source = f"impl X {{\n    fn f<T>(&self, {parameter}, next: Map<A, B>) {{ consume(old); }}\n}}\n"
            self.accepted(source, source.replace("old", "fresh"), "fn parameter")
        self.assertIsInstance(binding_scope("struct Foo {\n    old: T,\n}\n", BindingRename(2, "old: T,", "old", "fresh")),
                              NoBindingScope)

    def test_closure_block_expression_and_return_type(self) -> None:
        for closure in ("|old| consume(old)", "move |old: T| { consume(old); }", "|old| -> T { old }"):
            source = f"fn f() {{\n    let callback = {closure};\n    consume(old);\n}}\n"
            self.accepted(source, source.replace(closure, closure.replace("old", "fresh")), "closure")
        source = "fn f() {\n    consume(|old| old) + old;\n}\n"
        self.accepted(source, source.replace("|old| old", "|fresh| fresh"), "closure")
        self.assertFalse(self.prove(source, source.replace("old", "fresh")).explained)

    def test_for_pattern_is_not_in_scope_in_its_iterator_or_after_loop(self) -> None:
        source = "fn f() {\n    for (old, other) in old { consume(old); }\n    consume(old);\n}\n"
        self.accepted(source, source.replace("(old, other)", "(fresh, other)").replace("{ consume(old)",
                                                                                   "{ consume(fresh)"), "for")

    def test_shorthand_expands_in_expressions_and_patterns(self) -> None:
        source = "fn f() {\n    let old = input;\n    consume(Foo { old });\n}\n"
        self.accepted(source, source.replace("let old", "let fresh").replace("Foo { old }", "Foo { old: fresh }"), "let")
        source = "fn f() {\n    let Foo { old } = input;\n    consume(old);\n}\n"
        self.accepted(source, source.replace("Foo { old }", "Foo { old: fresh }").replace("consume(old)",
                                                                                      "consume(fresh)"), "let")

    def test_reverse_shorthand_refuses_the_existing_new_field_name(self) -> None:
        for source in ("fn f() {\n    let old = input;\n    consume(Foo { fresh: old });\n}\n",
                       "fn f() {\n    let Foo { fresh: old } = input;\n    consume(old);\n}\n"):
            edited = source.replace("let old", "let fresh").replace("fresh: old", "fresh").replace("consume(old)", "consume(fresh)")
            proof = self.prove(source, edited)
            self.assertEqual(tuple(proof.refusals.values()), ("new name already occurs in file",))
            self.assertFalse(proof.explained)

    def test_whole_file_collisions_include_shadowed_regions_constants_and_imports(self) -> None:
        sources = (
            "fn f() { let fresh = 10;\n let old = 1;\n { let old = 2; consume(fresh); }\n consume(old); }\n",
            "const fresh: i32 = 2; fn f() {\n let old = 1;\n consume(old); }\n",
            "use provider::fresh; fn f() {\n let old = 1;\n consume(old); }\n",
        )
        for source in sources:
            edited = source.replace("let old = 1", "let fresh = 1").replace("\n consume(old)", "\n consume(fresh)")
            proof = self.prove(source, edited)
            self.assertEqual(tuple(proof.refusals.values()), ("new name already occurs in file",))
            self.assertFalse(proof.explained)
        source = "fn f() {\n let old = 1;\n consume(old); }\nfn fresh_count() {}\n"
        self.accepted(source, source.replace("old", "fresh"), "let")

    def test_glob_import_refuses_a_potential_constant_pattern(self) -> None:
        source = "use provider::*; fn f() {\n let old = 1;\n consume(old); }\n"
        proof = self.prove(source, source.replace("old", "fresh"))
        self.assertEqual(tuple(proof.refusals.values()), ("file contains a glob import",))
        self.assertFalse(proof.explained)

    def test_old_item_or_type_refuses_the_entire_file(self) -> None:
        for header, body in (("type old = u8; type wide = u16;", "let value: old = 0; consume(old);"),
                             ("struct old;", "consume(old);"), ("fn old() {}", "consume(old);"),
                             ("use provider::old;", "consume(old);"),
                             ("use provider::{old as another};", "consume(old);"),
                             ("enum Kind { Item(old) }", "consume(old);"),
                             ("#[derive(old)] struct Kind;", "consume(old);"),
                             ("fn outer<old>() {}", "consume(old);"),
                             ("", "let value: external::old = input; consume(old);"),
                             ("", "let value = input as old; consume(old);")):
            source = f"{header} fn f() {{\n let old = 1;\n {body}\n}}\n"
            edited = source.replace("let old = 1", "let fresh = 1").replace(body, body.replace("old", "fresh"))
            proof = self.prove(source, edited)
            self.assertEqual(tuple(proof.refusals.values()), ("old name also occurs as an item or type",))
            self.assertFalse(proof.explained)

    def test_for_iterator_blocks_refuse_instead_of_proving_the_wrong_body(self) -> None:
        for iterator in ("if true { [1] } else { [2] }", "match x { _ => [1] }", "{ [1] }", "Items { value: 1 }"):
            source = f"fn f() {{ let old = 99;\n for old in {iterator} {{ consume(old); }}\n}}\n"
            edited = source.replace("for old", "for fresh")
            proof = self.prove(source, edited)
            self.assertEqual(tuple(proof.refusals.values()), ("for iterator contains a block before its body",))
            self.assertFalse(proof.explained)

    def test_capture_edits_refuse_macro_definitions_and_format_macro_imports(self) -> None:
        for header, call in (("macro_rules! println { ($text:literal) => { $text }; }", "println"),
                             ("fn r#macro_rules() {}", "println"),
                             ("use provider::println;", "println"),
                             ("use provider::{println};", "println"), ("", "provider::println")):
            source = f'{header} fn f() {{\n let old = 1;\n let text = {call}!("{{old}}");\n}}\n'
            proof = self.prove(source, source.replace("old", "fresh"))
            self.assertEqual(tuple(proof.refusals.values()), ("file defines or imports a possible format macro",))
            self.assertFalse(proof.explained)

    def test_macro_origins_refuse_identifier_edits_in_standard_named_calls(self) -> None:
        for header in ("macro_rules! vec { ($x:ident) => { stringify!($x) }; }",
                       "#[r#macro_use] mod provider;",
                       "macro vec($x:ident) { stringify!($x) }",
                       "use provider::vec;", "use provider::{custom as vec};",
                       "#[macro_use] extern crate provider;", "use provider::*;"):
            source = f"{header}\nfn f() {{\n let old = 1;\n (vec!(old), old)\n}}\n"
            proof = self.prove(source, source.replace("old", "fresh"), 3)
            with self.subTest(header=header):
                self.assertEqual(tuple(proof.checks.values()), (NoAppliedCheck("rename"),))
                self.assertFalse(proof.explained)
                if "*" not in header:
                    self.assertEqual(tuple(proof.refusals.values()), ("scope contains a macro with an uncertain origin",))
        source = "use provider::vec; fn elsewhere() { vec![1]; }\nfn f() {\n let old = 1;\n (vec![1], old)\n}\n"
        proof = self.prove(source, source.replace("old", "fresh"), 3)
        self.assertEqual(tuple(proof.checks.values()), (NoAppliedCheck("rename"),))
        self.assertFalse(proof.explained)
        source = "use provider::Vec;\nfn f() {\n let old = 1;\n (vec![old], old)\n}\n"
        self.accepted(source, source.replace("old", "fresh"), "let", 3)
        source = "macro_rules! vec { ($x:ident) => { stringify!($x) }; }\nfn f() {\n let old = 1;\n old\n}\n"
        self.accepted(source, source.replace("old", "fresh"), "let", 3)

    def test_condition_let_refuses_braces_in_the_scrutinee(self) -> None:
        for keyword in ("if", "while"):
            for scrutinee in ("make::<{1+1}>()", "(make::<{1+1}>())", "({ make() })"):
                source = ("fn f() { let old = 99;\n" +
                          f" {keyword} let Some(old) = {scrutinee} {{ return old; }}\n}}\n")
                proof = self.prove(source, source.replace("Some(old)", "Some(fresh)"))
                with self.subTest(keyword=keyword, scrutinee=scrutinee):
                    self.assertEqual(tuple(proof.checks.values()), (NoAppliedCheck("rename"),))
                    self.assertEqual(tuple(proof.refusals.values()), ("condition let scrutinee contains a block",))
                    self.assertFalse(proof.explained)
                    rename = BindingRename(2, source.splitlines()[1], "old", "fresh")
                    self.assertIsInstance(binding_scope(source, rename), NoBindingScope)
                    complete = source.replace("Some(old)", "Some(fresh)").replace("return old", "return fresh")
                    self.assertEqual(tuple(self.prove(source, complete).checks.values()), (NoAppliedCheck("rename"),))
        source = "fn f() { let old = 99;\n let Some(old) = make::<{1+1}>() else { return; };\n consume(old);\n}\n"
        proof = self.prove(source, source.replace("Some(old)", "Some(fresh)").replace("consume(old)", "consume(fresh)"))
        self.assertEqual(tuple(proof.checks.values()), (NoAppliedCheck("rename"),))
        self.assertFalse(proof.explained)
        source = "fn f() {\n let Some(old) = input else { return; };\n consume(old);\n}\n"
        self.accepted(source, source.replace("old", "fresh"), "let")

    def test_format_captures_only_change_in_format_arguments_and_keep_escaped_braces(self) -> None:
        for expression in ('println!("{{old}} {old:?}")', 'format!(r#"{old}"#)',
                           'write!(out, "{old}")', 'writeln!(out, "{old}")',
                           'assert!(ok, "{old}")', 'debug_assert!(ok, "{old}")',
                           'assert_eq!(a, b, "{old}")', 'debug_assert_ne!(a, b, "{old}")'):
            source = f"fn f() {{\n    let old = input;\n    {expression};\n}}\n"
            edited = source.replace("let old", "let fresh").replace("{old:", "{fresh:").replace('"{old}', '"{fresh}')
            self.accepted(source, edited, "let")
        for expression in ('consume("{old}")', 'vec!["{old}"]', 'write!("{old}", "text")',
                           'assert!("{old}")', 'assert_eq!("{old}", b)', 'format!("{}", "{old}")'):
            source = f"fn f() {{\n    let old = input;\n    {expression};\n}}\n"
            self.assertFalse(self.prove(source, source.replace("old", "fresh")).explained, expression)
        source = 'fn f() {\n    let old = input;\n    println!("{old:old$}");\n}\n'
        self.assertIsInstance(next(iter(self.prove(source, source.replace("let old", "let fresh").replace(
            '"{old:', '"{fresh:')).checks.values())), NoAppliedCheck)

    def test_shadowed_let_initializer_keeps_the_outer_binding(self) -> None:
        source = "fn f() {\n    let old = input;\n    consume(old);\n    let old = wrap(old);\n    consume(old);\n}\n"
        edited = source.replace("let old = input", "let fresh = input").replace("    consume(old);", "    consume(fresh);", 1)
        edited = edited.replace("wrap(old)", "wrap(fresh)")
        self.accepted(source, edited, "let")
        proof = self.prove(source, edited.replace("let old = wrap", "let fresh = wrap"))
        self.assertFalse(proof.explained)
        # The second declaration alone does not rename its initializer, whose binding is outer.
        inner = source.replace("let old = wrap(old);\n    consume(old)", "let fresh = wrap(old);\n    consume(fresh)")
        self.accepted(source, inner, "let", 4)

    def test_shadowing_parameters_closures_match_arms_and_for_patterns(self) -> None:
        for inner in ("let callback = |old| consume(old);", "for old in input { consume(old); }",
                      "match input { Some(old) => consume(old), _ => (), };", "fn inner(old: T) { consume(old); }"):
            source = f"fn f() {{\n    let old = input;\n    {inner}\n    consume(old);\n}}\n"
            edited = source.replace("let old = input", "let fresh = input").replace("\n    consume(old)", "\n    consume(fresh)")
            self.accepted(source, edited, "let")

    def test_each_refusal_leaves_the_finding_unproved_and_the_edit_unexplained(self) -> None:
        for tail, edited_tail in (("Foo { old: value }; consume(old);", "Foo { fresh: value }; consume(fresh);"),
                                  ("old.old();", "fresh.fresh();"),
                                  ("old + Module::old;", "fresh + Module::fresh;"),
                                  ("custom!(old);", "custom!(fresh);"),
                                  ("stringify!(old);", "stringify!(fresh);"),
                                  ("concat!(old);", "concat!(fresh);"),
                                  ("macro_rules! local { () => { old } }", "macro_rules! local { () => { fresh } }"),
                                  ("let fresh = input; consume(old);", "let fresh = input; consume(fresh);"),
                                  ("consume(old); consume(old);", "consume(fresh); consume(old);"),
                                  ("Foo { old };", "Foo { fresh };"),
                                  ('println!("{fresh}"); consume(old);', 'println!("{fresh}"); consume(fresh);')):
            source = f"fn f() {{\n    let old = input;\n    {tail}\n}}\n"
            edited = source.replace("let old", "let fresh").replace(tail, edited_tail)
            with self.subTest(tail=tail):
                proof = self.prove(source, edited)
                self.assertEqual(tuple(proof.checks.values()), (NoAppliedCheck("rename"),))
                self.assertTrue(proof.refusals)
                self.assertFalse(proof.explained)

    def test_locating_a_tie_or_multiple_declarations_refuses(self) -> None:
        source = "fn f() {\n    let old = input;\n} fn g() {\n    let old = input;\n}\n"
        rename = BindingRename(3, "let old = input;", "old", "fresh")
        self.assertEqual(binding_scope(source, rename), NoBindingScope("finding location is tied"))
        proof = binding_rename_check("lib.rs", source, source.replace("old", "fresh"), (rename,))
        self.assertEqual(proof.checks[rename], NoAppliedCheck("rename"))
        self.assertFalse(proof.explained)
        source = "fn f() {\n    let (old, old) = input;\n}\n"
        self.assertIsInstance(binding_scope(source, BindingRename(2, source.splitlines()[1], "old", "fresh")), NoBindingScope)

    def test_unapplied_declaration_only_and_unrelated_edit_have_distinct_results(self) -> None:
        source = "fn f() {\n    let old = input;\n    consume(old);\n}\n"
        self.assertEqual(tuple(self.prove(source, source).checks.values()), (RenameNotApplied("src/lib.rs"),))
        self.assertEqual(tuple(self.prove(source, source.replace("consume(old)", "consume(fresh)")).checks.values()),
                         (NoAppliedCheck("rename"),))
        proof = self.prove(source, source.replace("old", "fresh").replace("input", "other"))
        self.assertEqual(tuple(proof.checks.values()), (NoAppliedCheck("rename"),))
        self.assertFalse(proof.explained)

    def test_grouped_renames_with_the_same_old_name_in_different_arms(self) -> None:
        source = "fn f() { match input {\n    A(old) => consume(old),\n    B(old) => consume(old),\n} }\n"
        renames = (BindingRename(2, source.splitlines()[1], "old", "alpha"),
                   BindingRename(3, source.splitlines()[2], "old", "beta"))
        edited = source.replace("A(old) => consume(old)", "A(alpha) => consume(alpha)").replace(
            "B(old) => consume(old)", "B(beta) => consume(beta)")
        proof = binding_rename_check("lib.rs", source, edited, renames)
        self.assertEqual(tuple(proof.checks.values()), (AppliedInCode(), AppliedInCode()))
        self.assertTrue(proof.explained)
        partial = binding_rename_check("lib.rs", source, edited, renames[:1])
        self.assertFalse(partial.explained)

    def test_sibling_parameters_collide_even_when_the_new_name_is_unused(self) -> None:
        for declaration in ("fn f(old: T, fresh: T) { consume(old); }", "let f = |old, fresh| consume(old);"):
            source = f"impl X {{\n    {declaration}\n}}\n"
            proof = self.prove(source, source.replace("old", "fresh"))
            self.assertEqual(tuple(proof.checks.values()), (NoAppliedCheck("rename"),))
            self.assertFalse(proof.explained)

    def test_fn_generic_bounds_are_not_parameter_bindings(self) -> None:
        source = "impl X {\n    fn f<F: Fn(old)>(value: F) { consume(old); }\n}\n"
        self.assertIsInstance(binding_scope(source, BindingRename(2, source.splitlines()[1], "old", "fresh")), NoBindingScope)
        source = "impl X {\n    fn f<F: Fn(T)>(old: F) { consume(old); }\n}\n"
        self.accepted(source, source.replace("old", "fresh"), "fn parameter")

    def test_a_raw_use_of_the_old_name_cannot_be_missed(self) -> None:
        source = "fn f() {\n    let old = input;\n    consume(r#old);\n}\n"
        proof = self.prove(source, source.replace("let old", "let fresh"))
        self.assertEqual(tuple(proof.checks.values()), (NoAppliedCheck("rename"),))
        self.assertFalse(proof.explained)

    def test_encoded_format_braces_cannot_hide_a_missed_capture(self) -> None:
        source = 'fn f() {\n    let old = input;\n    println!("\\x7bold\\x7d");\n}\n'
        proof = self.prove(source, source.replace("let old", "let fresh"))
        self.assertEqual(tuple(proof.checks.values()), (NoAppliedCheck("rename"),))
        self.assertFalse(proof.explained)

    def test_nested_named_items_do_not_capture_an_outer_local(self) -> None:
        source = "fn f() {\n    let old = input;\n    fn inner() { consume(old); }\n    consume(old);\n}\n"
        edited = source.replace("let old", "let fresh").replace("\n    consume(old)", "\n    consume(fresh)")
        self.accepted(source, edited, "let")
        self.assertFalse(self.prove(source, source.replace("old", "fresh")).explained)

    def test_macro_local_bindings_and_ambiguous_condition_bodies_are_refused(self) -> None:
        for expression in ("vec![{ let old = 1; old }]", "matches!(input, Some(old) if old.ready())"):
            source = f"fn f() {{\n    let old = input;\n    {expression};\n    consume(old);\n}}\n"
            self.assertEqual(tuple(self.prove(source, source.replace("old", "fresh")).checks.values()),
                             (NoAppliedCheck("rename"),))
        source = "fn f() {\n    if let Some(old) = if ready { input } else { input } { consume(old); }\n}\n"
        self.assertIsInstance(binding_scope(source, BindingRename(2, source.splitlines()[1], "old", "fresh")), NoBindingScope)

    def test_comment_code_mentions_and_intra_doc_links_change_mechanically(self) -> None:
        source = ("fn f() {\n let old = 1;\n // `old` and [old] and [value](old).\n"
                  " /* `old` /* nested `old` */ */\n /// `old`\n consume(old);\n}\n")
        self.accepted(source, source.replace("old", "fresh"), "let")

    def test_parameter_owns_enclosing_function_docs_with_visibility_and_attributes(self) -> None:
        for prefix in ("", "pub ", "pub(crate) ", "#[inline]\npub ", 'pub unsafe extern "C" '):
            source = f"/// Use `old` and [old].\n{prefix}fn f(old: T) {{ consume(old); }}\n"
            line = next(i for i, text in enumerate(source.splitlines(), 1) if "fn f" in text)
            self.accepted(source, source.replace("old", "fresh"), "fn parameter", line)

    def test_stale_comment_mentions_fail_the_binding_check(self) -> None:
        for comment in ("// `old`", "/// [old]", "/** [value](old) */", "/* `old` */"):
            source = f"fn f() {{\n let old = 1; {comment}\n consume(old);\n}}\n"
            edited = source.replace("let old", "let fresh").replace("consume(old)", "consume(fresh)")
            proof = self.prove(source, edited)
            self.assertEqual(tuple(proof.checks.values()), (NoAppliedCheck("rename"),))
            self.assertIn("comment", next(iter(proof.refusals.values())))
            self.assertFalse(proof.explained)
        source = "/// Use `old`.\nfn f(old: T) { consume(old); }\n"
        proof = self.prove(source, source.replace("fn f(old", "fn f(fresh").replace("consume(old)", "consume(fresh)"))
        self.assertEqual(tuple(proof.checks.values()), (NoAppliedCheck("rename"),))
        self.assertFalse(proof.explained)

    def test_bare_comment_prose_requires_judgment(self) -> None:
        for comment in ("// Use old here.", "/// old is the input.", "/* old. */"):
            source = f"fn f() {{\n let old = 1;\n {comment}\n consume(old);\n}}\n"
            proof = self.prove(source, source.replace("old", "fresh"))
            self.assertEqual(tuple(proof.refusals.values()), ("prose names the binding",))
            self.assertFalse(proof.explained)

    def test_every_comment_edit_is_explained_including_outside_the_scope(self) -> None:
        source = "// unrelated\nfn f() {\n let old = 1;\n // `old` stays useful.\n consume(old);\n}\n"
        edited = source.replace("old", "fresh")
        for wrong in (edited.replace("unrelated", "changed"), edited.replace("stays useful", "is better"),
                      edited.replace("// unrelated\n", ""), edited + "// added\n",
                      edited.replace(" // `fresh` stays useful.\n", "").replace(" consume(fresh);", " consume(fresh); // `fresh` stays useful.")):
            self.assertFalse(self.prove(source, wrong, 3).explained, wrong)

    def test_next_function_docs_are_outside_a_parameters_scope(self) -> None:
        source = "fn f(old: T) { consume(old); }\n/// `old` belongs to the next function.\nfn other() {}\n"
        edited = source.replace("fn f(old", "fn f(fresh").replace("consume(old)", "consume(fresh)")
        self.accepted(source, edited, "fn parameter", 1)
        self.assertFalse(self.prove(source, edited.replace("`old`", "`fresh`"), 1).explained)

    def test_exact_text_names_preserve_prefixes_suffixes_case_and_path_forms(self) -> None:
        source = ("struct AlertKind; fn alert_kind() {}\nfn f() {\n let alert = 1;\n"
                  " // `alert` and `alert_count`; alerts, scopes, Alert, ALERT, alert-count, alert.rs, path/alert, ./alert, ../alert, alert::item.\n"
                  ' println!("{alert} {alert:?} {alert:>8} {alert_count} {alertx} {{alert}}");\n consume(alert);\n}\n')
        edited = source.replace("let alert", "let fresh").replace("`alert`", "`fresh`").replace("consume(alert)", "consume(fresh)")
        edited = edited.replace('"{alert}', '"{fresh}').replace("{alert:", "{fresh:")
        proof = self.prove(source, edited, 3, "alert")
        self.assertEqual(tuple(proof.checks.values()), (AppliedInCode(),))
        self.assertTrue(proof.explained)
        for old, new in (("alert_count", "fresh_count"), ("alerts", "freshs"), ("AlertKind", "FreshKind"),
                         ("Alert", "Fresh"), ("ALERT", "FRESH"), ("alert_kind", "fresh_kind"),
                         ("alert-count", "fresh-count"), ("alert.rs", "fresh.rs"), ("alertx", "freshx")):
            with self.subTest(old=old):
                self.assertFalse(self.prove(source, edited.replace(old, new), 3, "alert").explained)


class MachineRuleTests(unittest.TestCase):
    def test_machine_rule_contract_rejects_extra_conditions_in_body_or_metadata(self) -> None:
        from scripts.style.git_process import run_git
        original = approved_renames.TYPE_NAMED_PARAMETER_RULE
        for text, allowed in ((original, True), (original + "Only in functions returning bool.\n", False),
                              (original.replace("input_scope: file", "input_scope: file\nonly_return: bool"), False)):
            with self.subTest(text=text):
                blob = run_git(("git", "hash-object", "--stdin"), input=text.encode()).stdout.decode().strip()
                self.assertEqual(isinstance(approved_renames.machine_rename_rule(blob),
                                           approved_renames.TypeNamedParameterRule), allowed)

    def test_type_named_parameter_proof_requires_a_current_direct_named_type(self) -> None:
        for annotation, allowed in (("&ImplItem", True), ("&mut ImplItem", True),
                                    ("&crate::ImplItem", True), ("&OtherType", False),
                                    ("Vec<ImplItem>", False), ("impl Trait", False)):
            with self.subTest(annotation=annotation):
                source = "fn changed(item: " + annotation + ") { inspect(item); }\n"
                pair = BindingRename(1, source.strip(), "item", "impl_item")
                self.assertEqual(isinstance(approved_renames.type_named_parameter(source, pair), RenameDeclaration), allowed)


if __name__ == "__main__":
    _ = unittest.main()


class RefactorDeclarationTests(unittest.TestCase):
    def test_qualified_pair_is_available_only_to_the_refactor(self) -> None:
        pair = refactor_rename("rename", "Record::old", "Record::number", 1, "struct Record { old: usize }")
        self.assertIsInstance(pair, BindingRename)
        self.assertIsInstance(binding_rename("rename", "Record::old", "Record::number", 1, ""), NoBindingRename)
        assert isinstance(pair, BindingRename)
        self.assertIsInstance(rename_declaration(pair.text, pair), RenameDeclaration)

    def test_f001_sibling_parameters_and_same_pattern_collide(self) -> None:
        for source in ("fn f(old: usize, number: usize) {}",
                       "fn f(old: usize, r#number: usize) {}",
                       "fn f() { let closure = |old: usize, number: usize| {}; }",
                       "fn f() { let (old, number) = (1, 2); }"):
            pair = BindingRename(1, source, "old", "number")
            declaration = rename_declaration(source, pair)
            assert isinstance(declaration, RenameDeclaration)
            collision = pattern_binding_check(source, declaration, pair)
            self.assertIsInstance(collision, PatternBindingCollision)
            assert isinstance(collision, PatternBindingCollision)
            self.assertIn("'number'", collision.reason)

    def test_separate_parameter_lists_are_not_pattern_collisions(self) -> None:
        source = "fn f(old: usize) {}\nfn g(number: usize) {}"
        pair = BindingRename(1, source.splitlines()[0], "old", "number")
        declaration = rename_declaration(source, pair)
        assert isinstance(declaration, RenameDeclaration)
        self.assertIsInstance(pattern_binding_check(source, declaration, pair), DistinctPatternBindings)
