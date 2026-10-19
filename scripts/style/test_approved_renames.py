"""Decide from a file's names and declarations whether an approved rename is in the code."""

from __future__ import annotations

import unittest

import style_footprint
from approved_renames import (AppliedCheck, AppliedInCode, ApprovedRename, Declarations, NoAppliedCheck, RenameNotApplied,
                              UncertainScan, applied_check, approved_rename, declaration_sites)


def check(before: str, after: str, path: str, source: str) -> AppliedCheck:
    return applied_check("rename", before, after, path, source.encode())


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
        self.assertEqual(applied_check("type", "u32", "u64", "src/lib.rs", b"fn work() -> u64 { 1 }\n"),
                         NoAppliedCheck("type"))
        self.assertEqual(check("old_name", "new_name", "src/lib.rs", "fn new_name() {}\n"), NoAppliedCheck("rename"))


if __name__ == "__main__":
    _ = unittest.main()
