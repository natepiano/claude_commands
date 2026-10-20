"""A line of a Rust file reads as the item that declares it, so a held finding can follow its declaration."""

from __future__ import annotations

import sys
import declarations
import unittest
from pathlib import Path
from typing import final

sys.path.insert(0, str(Path(__file__).resolve().parent))

from declarations import Declaration, DeclaredItem, ImplOrTraitItem, ModuleItem, NoDeclarationAtLine, read_declarations

SOURCE = """use std::fmt;

/// The parent.
#[derive(Debug)]
pub struct Parent {
    old: u32,
}

impl fmt::Display for App {
    fn fmt(&self) -> u32 {
        1
    }
}
"""


def declaration(text: str, line: int) -> Declaration | NoDeclarationAtLine:
    read = read_declarations(text).declaration_at(line)
    return read.declaration if isinstance(read, DeclaredItem) else read


@final
class DeclarationTests(unittest.TestCase):
    def test_a_field_a_derive_and_a_doc_comment_line_read_their_struct(self) -> None:
        parent = Declaration("struct", "Parent", ModuleItem())
        self.assertEqual([declaration(SOURCE, line) for line in (6, 4, 3)], [parent] * 3)

    def test_a_method_line_of_a_trait_impl_reads_the_method_under_the_impl_type(self) -> None:
        self.assertEqual(declaration(SOURCE, 11), Declaration("fn", "fmt", ImplOrTraitItem("App")))

    def test_a_use_line_and_an_impl_header_line_read_no_declaration(self) -> None:
        self.assertEqual([declaration(SOURCE, line) for line in (1, 9)], [NoDeclarationAtLine()] * 2)

    def test_an_item_in_an_inline_module_and_at_a_file_top_level_are_one_declaration(self) -> None:
        inline, = (item.declaration for item in read_declarations("mod a {\n    struct X;\n}\n").declared_items if item.declaration.name == "X")
        self.assertEqual(inline, read_declarations("struct X;\n").declared_items[0].declaration)


    def test_trait_membership_preserves_the_implementing_type_and_reads_qualified_generic_headers(self) -> None:
        headers = (
            ("impl Trait for Type", ("Trait",)),
            ("impl<T> crate::traits::Trait<T> for Type<T>", ("crate", "traits", "Trait")),
            ("impl<T> Trait for Type<T> where T: for<'a> Fn(&'a str)", ("Trait",)),
            ("impl<T: for<'a> Fn(&'a str)> Trait for Type<T>", ("Trait",)),
        )
        for header, path in headers:
            with self.subTest(header=header):
                source = header + " {\n    fn method(&self) {}\n}\n"
                item = read_declarations(source).declaration_at(2)
                self.assertIsInstance(item, DeclaredItem)
                if isinstance(item, DeclaredItem):
                    self.assertEqual(item.declaration, Declaration("fn", "method", ImplOrTraitItem("Type")))
                    self.assertEqual(item.implemented_trait, declarations.ImplementedTrait(
                        path, ("<", "T", ">") if "Trait<T>" in header else ()))
                    self.assertEqual(declarations.declaration_line(item.declaration), "fn Type::method")

    def test_trait_arguments_distinguish_impls_and_ignore_only_whitespace(self) -> None:
        items = [read_declarations(header + " { fn method() {} }").declared_items[0].implemented_trait
                 for header in ("impl Trait<u8> for Type", "impl Trait<u16> for Type", "impl Trait < u8 > for Type")]
        self.assertNotEqual(items[0], items[1])
        self.assertEqual(items[0], items[2])

    def test_inherent_trait_body_and_nested_functions_do_not_inherit_trait_impl_membership(self) -> None:
        source = """impl Type {
    fn inherent() {}
}
trait Trait {
    fn required();
}
impl Trait for Type {
    fn method() {
        fn nested() {}
        { fn block_nested() {} }
    }
    const VALUE: u32 = {
        fn constant_nested() {}
        1
    };
}
"""
        items = {item.declaration.name: item for item in read_declarations(source).declared_items}
        self.assertEqual(items["inherent"].declaration.owner, ImplOrTraitItem("Type"))
        self.assertEqual(items["required"].declaration.owner, ImplOrTraitItem("Trait"))
        self.assertEqual(items["nested"].declaration.owner, ModuleItem())
        self.assertEqual([items[name].implemented_trait for name in ("inherent", "required", "nested", "block_nested", "constant_nested")],
                         [declarations.NoImplementedTrait()] * 5)
        self.assertEqual(items["method"].implemented_trait, declarations.ImplementedTrait(("Trait",)))

    def test_an_unreadable_trait_path_keeps_the_readable_impl_owner_without_claiming_membership(self) -> None:
        source = "impl <Type as Family>::Trait for Type {\n    fn method() {}\n}\n"
        item = read_declarations(source).declaration_at(2)
        self.assertIsInstance(item, DeclaredItem)
        if isinstance(item, DeclaredItem):
            self.assertEqual(item.declaration, Declaration("fn", "method", ImplOrTraitItem("Type")))
            self.assertEqual(item.implemented_trait, declarations.UnreadableImplementedTrait())


if __name__ == "__main__":
    _ = unittest.main()
