"""Candidate enumeration preserves source locations and rule constraints."""

from __future__ import annotations

import tempfile
import sqlite3
import os
import subprocess
from contextlib import closing
from unittest import mock
import unittest
from pathlib import Path
from typing import cast, final, override

from scripts.style import candidate_generators, style_ledger
from scripts.style.refill_facts import RefillFacts
from scripts.style.refill_inputs import RefillInputs, UnstoredFact, regex_tool_identity


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

        # Each input change uses the old filesystem enumeration as the oracle.
        _ = (self.root / "Cargo.toml").write_text(
            '[package]\nname="sample"\nversion="0.1.0"\n[workspace]\nmembers=["crates/*"]\n'
            + '[dependencies]\nbevy="1"\n'
        )
        member = self.root / "crates/alpha"
        (member / "src").mkdir(parents=True)
        _ = (member / "Cargo.toml").write_text(
            '[package]\nname="alpha"\nversion="0.1.0"\n[dependencies]\nhana_kana="1"\n'
        )
        _ = (member / "src/lib.rs").write_text("pub fn expose() -> Position {}\n")
        source = (
            '#[allow(dead_code)]\nfn spare() {}\n'
            + '#[allow(clippy::unwrap_used)]\nfn stale() {}\n'
            + 'struct Settings {\n state_enabled: bool,\n state_visible: bool,\n}\n'
            + 'struct UserProfile {\n user_count: usize,\n}\n'
            + 'enum TaskState {\n TaskStateOpen,\n}\ntrait Solo {}\nimpl Solo for One {}\n'
            + 'fn observer(event: On<Event>) {\n return;\n}\n'
            + 'fn magic() { let count = 42; let word = "marker"; value.unwrap(); }\n'
            + '#[cfg(test)]\nmod tests {\n fn check() { assert_eq!(value, 88); }\n}\n'
            + 'impl<T> Fresh for T {}\n'
        )
        _ = (self.root / "src/lib.rs").write_text(source)
        _ = (self.root / "src/utils.rs").write_text("fn marker() {}\n")
        _ = (self.root / "src/empty.rs").write_text("")
        _ = (self.root / "export_task.rs").write_text("fn task() {}\n")
        _ = subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        specs = tuple(candidate_generators.CandidatesSpec(kind=kind, pattern=r"unwrap\(|marker",
                                                         field_type_pattern=r"\bbool\b")
                      for kind in candidate_generators.GENERATORS) + (
            candidate_generators.CandidatesSpec(kind="regex", pattern=r"unwrap\(|marker", globs=("*.rs",),
                                                paths=("src",), paths_exempt=("src/generated",), exclude_pattern="approved"),
            candidate_generators.CandidatesSpec(kind="regex", pattern="marker", paths=("src/lib.rs",)),
            candidate_generators.CandidatesSpec(kind="regex", pattern="marker", paths=("src/lib.rs", "src/z.rs")),
            candidate_generators.CandidatesSpec(kind="regex", pattern=r"\p{Greek}", globs=("*.rs", "*.txt")),
            candidate_generators.CandidatesSpec(kind="regex", pattern="absent_marker"),
        )
        database = self.root.parent / (self.root.name + "-facts.sqlite3")
        self.addCleanup(database.unlink, missing_ok=True)
        connection = self.enterContext(closing(sqlite3.connect(database)))
        _ = connection.executescript(style_ledger.MIGRATIONS[40])
        _ = connection.executescript(style_ledger.MIGRATIONS[41])
        changes = ("initial", "blob", "new", "removed", "manifest", "ignore", "trait",
                   "ignore_edit", "ignore_remove", "manifest_add", "member_duplicate", "library_remove", "library_add")
        for change in changes:
            with self.subTest(change=change):
                if change == "blob":
                    _ = (self.root / "src/lib.rs").write_text(source + "impl Solo for Two {}\n")
                elif change == "new":
                    _ = (self.root / "src/fresh.rs").write_text(source)
                elif change == "removed":
                    (self.root / "src/fresh.rs").unlink()
                elif change == "manifest":
                    _ = (self.root / "Cargo.toml").write_text(
                        '[package]\nname="sample"\nversion="0.1.0"\n[workspace]\nmembers=[]\n'
                        + '[dependencies]\nhana_kana="1"\n'
                    )
                elif change == "ignore":
                    _ = (self.root / ".ignore").write_text("src/utils.rs\n")
                elif change == "trait":
                    _ = (self.root / "src/new_trait.rs").write_text("trait Fresh {}\n")
                elif change == "ignore_edit":
                    _ = (self.root / ".ignore").write_text("src/lib.rs\n")
                elif change == "ignore_remove":
                    (self.root / ".ignore").unlink()
                elif change == "manifest_add":
                    _ = (self.root / "Cargo.toml").write_text(
                        '[package]\nname="sample"\nversion="0.1.0"\n[workspace]\nmembers=["crates/*"]\n'
                        + '[dependencies]\nhana_kana="1"\n'
                    )
                    beta = self.root / "crates/beta"
                    (beta / "src").mkdir(parents=True)
                    _ = (beta / "Cargo.toml").write_text(
                        '[package]\nname="beta"\nversion="0.1.0"\n[dependencies]\nbevy="1"\n'
                    )
                    _ = (beta / "src/lib.rs").write_text("pub fn marker() {}\n")
                elif change == "member_duplicate":
                    _ = (self.root / "Cargo.toml").write_text(
                        '[package]\nname="sample"\nversion="0.1.0"\n[workspace]\nmembers=[".","crates/*","crates/*"]\n'
                        + '[dependencies]\nhana_kana="1"\n'
                    )
                elif change == "library_remove":
                    (member / "src/lib.rs").unlink()
                elif change == "library_add":
                    _ = (member / "src/lib.rs").write_text("pub fn expose() -> Position {}\n")
                _ = subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
                tree = subprocess.run(["git", "-C", str(self.root), "write-tree"], check=True,
                                      capture_output=True, text=True).stdout.strip()
                facts = RefillFacts(connection, str(self.root / ".git"), "test")
                inputs = RefillInputs(self.root, tree, facts)
                with tempfile.TemporaryDirectory() as full:
                    export = Path(full) / "export"
                    for path, content in inputs.sources(tuple(inputs.blobs)):
                        target = export / path
                        target.parent.mkdir(parents=True, exist_ok=True)
                        _ = target.write_bytes(content)
                    for index, current in enumerate(specs):
                        with self.subTest(generator=current.kind, index=index):
                            expected = candidate_generators.enumerate_candidates(current, export)
                            actual = facts.enumerate(f"{index}:{current.kind}", current, inputs)
                            self.assertEqual(actual, expected)
                            if current.kind == "submodule_names":
                                self.assertIn("export_task.rs", tuple(site.file for site in actual.candidates))
                    expected_filter = tuple(candidate_generators.Candidate(path, line, text)
                                            for path in sorted(inputs.blobs) if path.endswith(".rs")
                                            for line, text in enumerate((export / path).read_text().splitlines(), 1)
                                            if "marker" in text)
                    self.assertEqual(facts.pre_filter("pre", "marker", inputs,
                                                     tuple(sorted(path for path in inputs.blobs if path.endswith(".rs")))),
                                     expected_filter)
                self.assertEqual(facts.pre_filter("empty-pre", "marker", inputs, ()), ())
                self.assertTrue(bool(connection.execute(
                    "SELECT 1 FROM refill_candidates WHERE rule='empty-pre' AND partition='@empty'").fetchall()))
                facts.finish()
                connection.commit()
                warm = RefillFacts(connection, str(self.root / ".git"), "test")
                with mock.patch.object(RefillInputs, "read_trees", side_effect=AssertionError("unchanged tree")):
                    warm_inputs = RefillInputs(self.root, tree, warm)
                with mock.patch.object(candidate_generators, "enumerate_candidates", side_effect=AssertionError("warm generator")), \
                     mock.patch.object(candidate_generators, "mask_source", side_effect=AssertionError("warm mask")), \
                     mock.patch.object(candidate_generators, "trait_impl_count", side_effect=AssertionError("warm trait")), \
                     mock.patch.object(RefillInputs, "regex", side_effect=AssertionError("warm regex")):
                    for index, current in enumerate(specs):
                        _ = warm.enumerate(f"{index}:{current.kind}", current, warm_inputs)
                    self.assertEqual(warm.pre_filter("pre", "marker", warm_inputs,
                                                     tuple(sorted(path for path in inputs.blobs if path.endswith(".rs")))),
                                     expected_filter)
                warm.finish()
                connection.commit()
        empty = cast(tuple[int], connection.execute("SELECT COUNT(*) FROM refill_candidates WHERE sites LIKE '%[]%'").fetchone())
        self.assertIsNotNone(empty)
        self.assertGreater(empty[0], 0)

        tree = subprocess.run(["git", "-C", str(self.root), "write-tree"], check=True,
                              capture_output=True, text=True).stdout.strip()
        config = self.root.parent / (self.root.name + "-ripgrep.conf")
        self.addCleanup(config.unlink, missing_ok=True)
        configured = candidate_generators.CandidatesSpec(kind="regex", pattern="marker")
        with mock.patch.dict(os.environ, {"RIPGREP_CONFIG_PATH": str(config)}):
            missing_identity = regex_tool_identity()
            for excluded in ("", "src/lib.rs", "src/new.rs"):
                if excluded:
                    _ = config.write_text("--glob\n!" + excluded + "\n")
                    os.utime(config, ns=(1700000000000000000, 1700000000000000000))
                    self.assertNotEqual(regex_tool_identity(), missing_identity)
                facts = RefillFacts(connection, str(self.root / ".git"), "configured")
                inputs = RefillInputs(self.root, tree, facts)
                with tempfile.TemporaryDirectory() as full:
                    export = Path(full) / "export"
                    for path, content in inputs.sources(tuple(inputs.blobs)):
                        target = export / path
                        target.parent.mkdir(parents=True, exist_ok=True)
                        _ = target.write_bytes(content)
                    expected = candidate_generators.enumerate_candidates(configured, export)
                    self.assertEqual(facts.enumerate("regex", configured, inputs), expected)
                facts.finish()
                connection.commit()


        _ = (self.root / "src/invalid.rs").write_bytes(b"\xff")
        _ = subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        tree = subprocess.run(["git", "-C", str(self.root), "write-tree"], check=True,
                              capture_output=True, text=True).stdout.strip()
        facts = RefillFacts(connection, str(self.root / ".git"), "invalid")
        inputs = RefillInputs(self.root, tree, facts)
        with tempfile.TemporaryDirectory() as full:
            export = Path(full) / "export"
            for path, content in inputs.sources(tuple(inputs.blobs)):
                target = export / path
                target.parent.mkdir(parents=True, exist_ok=True)
                _ = target.write_bytes(content)
            for index, current in enumerate(specs):
                with self.subTest(generator=current.kind, input="invalid UTF-8"):
                    expected = candidate_generators.enumerate_candidates(current, export)
                    self.assertEqual(facts.enumerate(f"{index}:{current.kind}", current, inputs), expected)
        invalid_blob = inputs.blobs["src/invalid.rs"]
        self.assertIsInstance(facts.fact(invalid_blob, "source_text"), UnstoredFact)
        self.assertIsInstance(facts.fact(invalid_blob, "masked_source"), UnstoredFact)
        with self.assertRaises(UnicodeDecodeError):
            _ = facts.pre_filter("invalid-filter", "marker", inputs, ("src/invalid.rs",))

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
