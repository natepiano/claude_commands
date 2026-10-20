"""A line of a Rust file reads as the item that declares it, so a held finding can follow its declaration."""

from __future__ import annotations

import sys
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


if __name__ == "__main__":
    _ = unittest.main()
