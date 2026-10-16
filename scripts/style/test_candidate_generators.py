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
            "---\ncandidates:\n  kind: regex\n"
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
            "---\ncandidates:\n  kind: struct_fields\n  structs: [Settings]\n"
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


if __name__ == "__main__":
    _ = unittest.main()
