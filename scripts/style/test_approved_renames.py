"""Decide from a file's names and declarations whether an approved rename is in the code."""

from __future__ import annotations

import unittest

import style_footprint
from approved_renames import (AppliedCheck, AppliedInCode, ApprovedRename, BindingProof, BindingRename, BindingScope,
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


class AppliedCheckTests(unittest.TestCase):
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
