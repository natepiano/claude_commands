"""Candidate enumeration preserves source locations and rule constraints."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import final, override

import candidate_generators


@final
class CandidateGeneratorTests(unittest.TestCase):
    root: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.root / "src").mkdir()
        _ = (self.root / "Cargo.toml").write_text(
            '[package]\nname = "sample"\nversion = "0.1.0"\nedition = "2021"\n'
        )

    def test_regex_rule_filters_paths_and_lines_and_sorts_sites(self) -> None:
        rule = self.root / "rule.md"
        _ = rule.write_text(
            "---\ncandidates:\n  kind: regex\n  surface: whole\n"
            + "  pattern: 'unwrap\\('\n  exclude_pattern: 'approved'\n"
            + "  globs: ['*.rs']\n  paths: [src]\n"
            + "  paths_exempt: [src/generated]\n---\n"
        )
        _ = (self.root / "src" / "z.rs").write_text("fn z() { last.unwrap(); }\n")
        _ = (self.root / "src" / "lib.rs").write_text(
            "fn run() {\n    first.unwrap();\n    second.unwrap(); // approved\n}\n"
        )
        (self.root / "src" / "generated").mkdir()
        _ = (self.root / "src" / "generated" / "bindings.rs").write_text("value.unwrap();\n")
        _ = (self.root / "src" / "notes.txt").write_text("value.unwrap();\n")
        _ = (self.root / "outside.rs").write_text("value.unwrap();\n")

        spec = candidate_generators.read_candidates_spec(rule)
        if spec is None:
            self.fail("The regex rule's candidates block was not read")
        result = candidate_generators.enumerate_candidates(spec, self.root)

        self.assertEqual(
            [(site.file, site.line, site.text) for site in result.candidates],
            [("src/lib.rs", 2, "first.unwrap();"), ("src/z.rs", 1, "fn z() { last.unwrap(); }")],
        )
        self.assertEqual(result.source, r"regex:unwrap\(")

    def test_struct_field_rule_limits_structs_and_field_types(self) -> None:
        rule = self.root / "rule.md"
        _ = rule.write_text(
            "---\ncandidates:\n  kind: struct_fields\n  surface: whole\n  structs: [Settings]\n"
            + "  field_type_pattern: '\\bbool\\b'\n---\n"
        )
        _ = (self.root / "src" / "lib.rs").write_text(
            "pub struct Settings {\n    pub enabled: bool,\n    count: usize,\n"
            + "    // commented: bool,\n}\n"
            + "struct Other {\n    enabled: bool,\n}\n"
            + "fn check(enabled: bool) {}\n"
        )

        spec = candidate_generators.read_candidates_spec(rule)
        if spec is None:
            self.fail("The struct-field rule's candidates block was not read")
        result = candidate_generators.enumerate_candidates(spec, self.root)

        self.assertEqual(
            [(site.file, site.line, site.text) for site in result.candidates],
            [("src/lib.rs", 2, "struct Settings: field `enabled: bool`")],
        )

    def test_enumeration_returns_all_200_sites(self) -> None:
        _ = (self.root / "src" / "lib.rs").write_text(
            "".join(f"fn site_{index}() {{ value.unwrap(); }}\n" for index in range(200))
        )
        spec = candidate_generators.CandidatesSpec(kind="regex", pattern=r"unwrap\(")

        result = candidate_generators.enumerate_candidates(spec, self.root)

        self.assertEqual(len(result.candidates), 200)
        self.assertEqual([site.line for site in result.candidates], list(range(1, 201)))
        self.assertEqual({site.file for site in result.candidates}, {"src/lib.rs"})
        self.assertEqual(result.candidates[-1].text, "fn site_199() { value.unwrap(); }")

    def test_legacy_surface_preserves_enumeration_and_rule_constraints(self) -> None:
        rule = self.root / "rule.md"
        _ = (self.root / "src" / "lib.rs").write_text(
            "fn run() {\n    first.unwrap();\n    second.unwrap(); // approved\n}\n"
        )
        _ = (self.root / "outside.rs").write_text("outside.unwrap();\n")
        declarations = (
            ("", "unclassified"),
            ("  surface: whole\n", "whole"),
            ("  surface: partial\n  uncovered: 'other methods'\n", "partial"),
        )
        for declaration, expected_surface in declarations:
            with self.subTest(surface=expected_surface):
                _ = rule.write_text(
                    "---\nmechanism: llm\ncandidates:\n  kind: regex\n"
                    + declaration
                    + "  pattern: 'unwrap\\('\n  exclude_pattern: approved\n  paths: [src]\n---\n"
                )
                spec = candidate_generators.read_candidates_spec(rule)
                if spec is None:
                    self.fail("The legacy candidates block was not read")

                result = candidate_generators.enumerate_candidates(spec, self.root)

                self.assertEqual(spec.surface, expected_surface)
                self.assertEqual(
                    [(site.file, site.line, site.text) for site in result.candidates],
                    [("src/lib.rs", 2, "first.unwrap();")],
                )
                self.assertEqual(result.source, r"regex:unwrap\(")

    def test_a_cfg_test_item_spans_its_body_and_an_item_with_no_body_gives_none(self) -> None:
        text = (
            "#[cfg(test)]\nmod tests {\n    fn f() {}\n}\n"
            + "#[cfg(test)]\nfn helper(x: u8) -> [u8; 2] {\n    [x, x]\n}\n"
            + "#[cfg(test)]\nfn bound<T>(x: T)\nwhere\n    T: Clone,\n    T: Copy,\n{\n    let _ = x;\n}\n"
            + "#[cfg(test)]\nmod more;\n#[cfg(test)]\nuse super::{a, b};\n#[cfg(test)]\nconst LIMIT: u8 = { 3 };\n"
            + "enum Shape {\n    #[cfg(test)]\n    Dot,\n    Box {\n        side: u8,\n    },\n}\n"
        )
        lines = candidate_generators.mask_source(text).lines
        self.assertEqual(candidate_generators.test_spans(lines), [(1, 4), (5, 8), (9, 16)])

    def test_literal_generators_read_cfg_test_items(self) -> None:
        _ = (self.root / "src" / "lib.rs").write_text(
            "pub fn run() -> u32 {\n    let base = 42;\n    base\n}\n\n"
            + "#[cfg(test)]\nfn helper(x: u32) -> u32 {\n    assert!(x != 66);\n    x + 77\n}\n\n"
            + "#[cfg(test)]\nmod tests {\n    #[test]\n    fn checks() {\n        assert_eq!(super::helper(1), 78);\n"
            + "    }\n}\n"
        )
        literals = candidate_generators.enumerate_candidates(candidate_generators.CandidatesSpec(kind="literals"),
                                                             self.root)
        tests = candidate_generators.enumerate_candidates(candidate_generators.CandidatesSpec(kind="test_literals"),
                                                          self.root)

        self.assertEqual([site.line for site in literals.candidates], [2])
        self.assertEqual([site.line for site in tests.candidates], [8, 16])

    def test_an_allow_scope_is_the_item_body_after_a_const_block_in_its_signature(self) -> None:
        body = " {\n    value.unwrap()\n}\n"
        for signature in ("fn f() -> S<{ N }>", "fn f<const N: usize>(a: [u8; { N }])",
                          "fn f<T>() where T: Tr<{ N }>", "impl<T: Into<u8>> Tr for S<{ N }> where T: Fn() -> u8"):
            with self.subTest(signature=signature):
                masked = candidate_generators.mask_source("#[allow(clippy::unwrap_used)]\n" + signature + body)
                self.assertEqual(candidate_generators.boilerplate_allows(masked),
                                 (candidate_generators.BoilerplateAllow(line=1, column=0, stale=(), local=True),))
        arm = ("match shape {\n    #[allow(clippy::unwrap_used)]\n    Shape::Box { side }{guard} => {\n"
               + "        side.unwrap()\n    }\n}\n")
        for guard in ("", " if side.is_some()", " | Shape::Line { side }"):
            with self.subTest(guard=guard):
                allows = candidate_generators.boilerplate_allows(candidate_generators.mask_source(
                    arm.replace("{guard}", guard)))
                self.assertEqual([(allow.stale, allow.local) for allow in allows], [((), False)])
        for item in ("mod tests", "fn f(a: u8) -> u8"):
            with self.subTest(item=item):
                masked = candidate_generators.mask_source(
                    "#[allow(clippy::unwrap_used, clippy::panic)]\n" + item + body + "fn g() { panic!() }\n")
                self.assertEqual(
                    candidate_generators.boilerplate_allows(masked),
                    (candidate_generators.BoilerplateAllow(line=1, column=0, stale=("clippy::panic",), local=True),))

    def test_an_allow_scope_is_the_whole_item_after_the_attribute(self) -> None:
        length = "fn length(value: Option<usize>) -> usize { value.unwrap() } 1"
        for item, local in (
            ("#[cfg(test)] #[allow(clippy::unwrap_used)] fn f() -> [u8; { " + length + " }] { [0] }\n", True),
            ("#[allow(clippy::unwrap_used)]\nfn f() -> [u8; {\n    " + length + "\n}] {\n    [0]\n}\n", True),
            ("#[allow(clippy::unwrap_used)]\nstruct S<const N: usize = { Some(3).unwrap() }> {\n    a: [u8; N],\n}\n",
             True),
            ("#[allow(clippy::unwrap_used)]\nfn f<T>()\nwhere\n    T: Tr<{ Some(3).unwrap() }>,\n    T: Copy,\n{\n}\n",
             True),
            ("#[allow(clippy::unwrap_used)]\n#[doc = include_str!(\"a.md\")]\n"
             + "fn f() -> [u8; {\n    mod x;\n    1\n}] {\n    Some([0]).unwrap()\n}\n", False),
        ):
            with self.subTest(item=item):
                allows = candidate_generators.boilerplate_allows(candidate_generators.mask_source(item))
                self.assertEqual([(allow.stale, allow.local) for allow in allows], [((), local)])

    def test_an_include_word_or_a_macro_item_in_the_file_makes_no_scope_local(self) -> None:
        scope = "#[allow(clippy::unwrap_used)]\nmod tests {\n    fn f() {\n        {call}\n    }\n}\n"
        for head, call, local in (
            ("use std::include as load;\n", 'load!("helpers.rs");', False),
            ("use std::include;\n", "", False),
            ("", 'std::r#include!("helpers.rs");', False),
            ("macro_rules! open {\n    ($x:expr) => { $x.unwrap() };\n}\n", "open!(value);", False),
            ("", 'let _ = include_str!("helpers.rs");', True),
        ):
            with self.subTest(head=head, call=call):
                masked = candidate_generators.mask_source(head + scope.replace("{call}", call))
                self.assertEqual([allow.local for allow in candidate_generators.boilerplate_allows(masked)], [local])


if __name__ == "__main__":
    _ = unittest.main()
