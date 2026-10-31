"""Check assertion-guard behavior against real Git repositories."""
from __future__ import annotations

import os
import resource
import subprocess
import sys
import time
import unittest
from pathlib import Path
from typing import NamedTuple, override
from unittest.mock import patch

from scripts.direct.assertion_guard import commit_bases, default_base
from scripts.production import showrunners
from scripts.direct.assertion_guard_test_support import GUARD, AssertionFindingFixture, ComparisonBases, SeededGitRepositoryFixture, SeededMergeInProgressFixture, CLEAN, TILE_OLD, TILE_NEW

class AssertionRevisionTests(AssertionFindingFixture):
    def test_deleted_python_test_with_different_parent_signatures_still_blocks(self) -> None:
        before = ("def test_outer():\n    def final_diff():\n        return 1\n"
                  + "    assert final_diff() == 1\n")
        bases = self.change_against_bases("test_x.py", before,
                                         before.replace("test_outer():", "test_outer(tmp_path):"),
                                         "# removed test\n")
        for first, second in (bases, ComparisonBases(bases.second, bases.first)):
            with self.subTest(first=first):
                line = self.assert_finding(self.guard("--base", first, "--base", second),
                                           "test_x.py", "removed", "test_outer")
                self.assertIn("assert final_diff() == 1 -> (none)", line)
                _ = self.assert_finding(self.guard("--base", first, "--base", second,
                    "--rulings", "-", stdin="Assertion-Change: test_x.py::test_outer — Approved removal.\n"),
                    "test_x.py", "removed", "test_outer", status=0)


    def test_deleted_rust_test_with_different_parent_return_types_still_blocks(self) -> None:
        for gap in ("", "\n\n"):
            before = ("#[test]\nfn outer() -> Result<(), [u8; 1]> {\n" + gap
                      + "    fn helper() {}\n    assert_eq!(1, 1);\n    Ok(())\n}\n")
            bases = self.change_against_bases("value.rs", before, before.replace("[u8; 1]", "u8"),
                                             "// removed test\n")
            for first, second in (bases, ComparisonBases(bases.second, bases.first)):
                with self.subTest(first=first, gap=gap):
                    line = self.assert_finding(self.guard("--base", first, "--base", second),
                                               "value.rs", "removed", "outer")
                    self.assertIn("assert_eq!(1, 1); -> (none)", line)
                    _ = self.assert_finding(self.guard("--base", first, "--base", second,
                        "--rulings", "-", stdin="Assertion-Change: value.rs::outer — Approved removal.\n"),
                        "value.rs", "removed", "outer", status=0)


    def test_deleted_test_counts_only_assertions_shared_by_every_base(self) -> None:
        bases = self.change_against_bases("test_x.py",
            self.python("assert shared == expected\n    assert first == expected", "test_outer"),
            self.python("assert shared == expected\n    assert second == expected", "test_outer"),
            "# removed test\n")
        for first, second in (bases, ComparisonBases(bases.second, bases.first)):
            with self.subTest(first=first):
                result = self.guard("--base", first, "--base", second)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertEqual(result.stderr, "")
                findings = [line for line in result.stdout.splitlines() if not line.startswith("assertion guard:")]
                self.assertEqual(len(findings), 1, result.stdout)
                self.assertRegex(findings[0], r"^test_x\.py:\d+ test_outer test removed \(1 assertions\): ")
                self.assertIn("assertion guard: 1 blocking;", result.stdout)


    def test_parse_failure_keeps_shared_removal_and_first_base_owner(self) -> None:
        before = self.python("def final_diff():\n        return 1\n    assert final_diff() == 1", "test_outer")
        other = before + '\nlabel = t"hello"\n'
        bases = self.change_against_bases("test_x.py", before, other,
                                         other.replace("    assert final_diff() == 1\n", "    pass\n"))
        for first, second, owner in ((bases.first, bases.second, "test_outer"),
                                     (bases.second, bases.first, "final_diff")):
            with self.subTest(first=first):
                _ = self.assert_finding(self.guard("--base", first, "--base", second),
                                        "test_x.py", "removed", owner)


    def test_fixture_attributes_do_not_own_outside_helper_assertions(self) -> None:
        for attribute in ("#[rstest::fixture]", "#[fixture]"):
            with self.subTest(attribute=attribute):
                before = (attribute + "\nfn client() -> usize {\n"
                          + "    fn helper() -> usize { 1 }\n    assert_eq!(helper(), 1);\n    helper()\n}\n")
                self.change("value.rs", before, before.replace("    assert_eq!(helper(), 1);\n", ""))
                _ = self.assert_finding(self.guard("--rulings", "-", stdin=(
                    "Assertion-Change: value.rs::client — Approved test removal.\n")),
                    "value.rs", "removed", "helper")


    def test_owner_changes_do_not_hide_shared_weaker_assertions(self) -> None:
        before = self.python("def final_diff():\n        return 1\n    assert final_diff() == 1", "test_outer")
        other = before + '\nlabel = t"hello"\n'
        bases = self.change_against_bases("test_x.py", before, other,
                                         other.replace("assert final_diff() == 1", "assert final_diff() >= 1"))
        for first, second, owner in ((bases.first, bases.second, "test_outer"),
                                     (bases.second, bases.first, "final_diff")):
            with self.subTest(first=first):
                _ = self.assert_finding(self.guard("--base", first, "--base", second),
                                        "test_x.py", "weaker", owner)


    def test_signature_semicolons_preserve_test_owner_and_shared_removal(self) -> None:
        before = ("#[test]\nfn outer() -> Result<(), [u8; 1]> {\n"
                  + "    fn helper() {}\n    assert_eq!(1, 1);\n    Ok(())\n}\n")
        other = before.replace("[u8; 1]", "u8")
        bases = self.change_against_bases("value.rs", before, other,
                                         other.replace("    assert_eq!(1, 1);\n", ""))
        _ = self.assert_finding(self.guard("--base", bases.first), "value.rs", "removed", "outer")
        _ = self.assert_finding(self.guard("--base", bases.first, "--rulings", "-", stdin=(
            "Assertion-Change: value.rs::outer — Approved test removal.\n")),
            "value.rs", "removed", "outer", status=0)
        for first, second in (bases, ComparisonBases(bases.second, bases.first)):
            with self.subTest(first=first):
                _ = self.assert_finding(self.guard("--base", first, "--base", second),
                                        "value.rs", "removed", "outer")


    def test_spaced_and_closed_test_attributes_own_nested_helpers(self) -> None:
        for attribute in ("#[tokio :: test]", "# [test]", "# [ async_std :: test ]",
                          "#[rstest :: rstest]", "#[test_case(1)]", "#[test_case :: test_case(1)]"):
            with self.subTest(attribute=attribute):
                before = (attribute + "\nasync fn outer() {\n"
                          + "    fn helper() {}\n    assert_eq!(1, 1);\n}\n")
                self.change("value.rs", before, before.replace("    assert_eq!(1, 1);\n", ""))
                _ = self.assert_finding(self.guard(), "value.rs", "removed", "outer")
                _ = self.assert_finding(self.guard("--rulings", "-", stdin=(
                    "Assertion-Change: value.rs::outer — Approved test removal.\n")),
                    "value.rs", "removed", "outer", status=0)


    def test_unbalanced_braces_keep_baseline_helper_owner(self) -> None:
        for braces in ("", "}\n}\n"):
            with self.subTest(braces=braces):
                before = ("#[test]\nfn outer() {\n    fn helper() {}\n"
                          + "    assert_eq!(1, 1);\n" + braces)
                self.change("value.rs", before, before.replace("    assert_eq!(1, 1);\n", ""))
                _ = self.assert_finding(self.guard("--rulings", "-", stdin=(
                    "Assertion-Change: value.rs::outer — Approved test removal.\n")),
                    "value.rs", "removed", "helper")


    def test_python_assertion_after_nested_helper_uses_enclosing_test_and_ruling(self) -> None:
        for declaration in ("def", "async def"):
            with self.subTest(declaration=declaration):
                before = ("import unittest\nclass T(unittest.TestCase):\n"
                          + f"    {declaration} test_outer(self):\n"
                          + "        def final_diff():\n            return 1\n"
                          + "        self.assertEqual(final_diff(), 1)\n        self.assertTrue(True)\n")
                self.change("test_x.py", before, before.replace("        self.assertTrue(True)\n", ""))
                _ = self.assert_finding(self.guard(), "test_x.py", "removed", "test_outer")
                result = self.guard("--rulings", "-", stdin=(
                    "Assertion-Change: test_x.py::test_outer — Approved removal.\n"))
                line = self.assert_finding(result, "test_x.py", "removed", "test_outer", status=0)
                self.assertIn("ruled: Approved removal.", line)


    def test_rust_assertion_after_nested_helper_uses_enclosing_test_and_ruling(self) -> None:
        for attribute in ("#[test]", "#[tokio::test]", "#[async_std::test]", "#[rstest]"):
            with self.subTest(attribute=attribute):
                before = (attribute + "\nfn outer() {\n"
                          + "    fn final_diff() -> usize { 1 }\n"
                          + "    assert_eq!(final_diff(), 1);\n    assert!(true);\n}\n")
                self.change("value.rs", before, before.replace("    assert!(true);\n", ""))
                _ = self.assert_finding(self.guard(), "value.rs", "removed", "outer")
                result = self.guard("--rulings", "-", stdin=(
                    "Assertion-Change: value.rs::outer — Approved removal.\n"))
                line = self.assert_finding(result, "value.rs", "removed", "outer", status=0)
                self.assertIn("ruled: Approved removal.", line)


    def test_assertion_inside_nested_helper_uses_enclosing_test(self) -> None:
        for path, before, assertion in (
            ("test_x.py", self.python("def final_diff():\n        assert actual == expected\n"
                                      + "        return 1\n    final_diff()", "test_outer"),
             "        assert actual == expected\n"),
            ("value.rs", self.rust("fn final_diff() {\n        assert_eq!(actual, expected);\n"
                                   + "    }\n    final_diff();", "test_outer"),
             "        assert_eq!(actual, expected);\n"),
        ):
            with self.subTest(path=path):
                self.change(path, before, before.replace(assertion, ""))
                _ = self.assert_finding(self.guard(), path, "removed", "test_outer")


    def test_test_scope_ends_before_an_outside_helper(self) -> None:
        for path, before, assertion in (
            ("test_x.py", self.python("def nested():\n        return 1\n    assert True", "test_outer")
             + "\n" + self.python("assert actual == expected", "outside_helper"),
             "    assert actual == expected\n"),
            ("value.rs", self.rust("fn nested() {}\n    assert!(true);", "outer")
             + "\nfn outside_helper() {\n    assert_eq!(actual, expected);\n}\n",
             "    assert_eq!(actual, expected);\n"),
        ):
            with self.subTest(path=path):
                self.change(path, before, before.replace(assertion, "    pass\n" if path.endswith(".py") else ""))
                _ = self.assert_finding(self.guard(), path, "removed", "outside_helper")


    def test_innermost_test_owns_only_its_body(self) -> None:
        for path, before, inner, outer in (
            ("test_x.py", self.python("def test_inner():\n        assert inner_ok\n"
                                      + "        return 1\n    assert outer_ok", "test_outer"),
             "        assert inner_ok\n", "    assert outer_ok\n"),
            ("value.rs", self.rust("#[test]\n    fn test_inner() {\n        assert!(inner_ok);\n"
                                   + "    }\n    assert!(outer_ok);", "test_outer"),
             "        assert!(inner_ok);\n", "    assert!(outer_ok);\n"),
        ):
            for assertion, owner in ((inner, "test_inner"), (outer, "test_outer")):
                with self.subTest(path=path, owner=owner):
                    self.change(path, before, before.replace(assertion, ""))
                    _ = self.assert_finding(self.guard(), path, "removed", owner)


    def test_module_path_import_allows_pairing_but_statement_macro_refuses(self) -> None:
        prefix = ('mod module_path {}\nmod tests {\nuse super::module_path;\n'
                  + 'const FIELD: &str = "strict";\n')
        for statement in ("", "module_path!();\n    "):
            with self.subTest(statement=statement):
                self.change("value.rs", prefix + self.rust(statement + 'assert!(stdout.contains("strict"));')
                            + "}\n", prefix + self.rust(statement + 'assert!(stdout.contains(FIELD));') + "}\n")
                if statement:
                    _ = self.assert_finding(self.guard(), "value.rs", "removed")
                else:
                    self.assert_clean(self.guard())


    def test_matches_import_alias_refuses_range_endpoint_pairing(self) -> None:
        for imports, support in (
            ("use std::stringify as matches;\n", ""),
            ("use ::std::stringify as matches;\n", ""),
            ("use ::core::{stringify as matches};\n", ""),
            ("use external::matches;\n", ""),
            ("use std::{stringify as matches};\n", ""),
            ("mod support;\nuse support::*;\n", "pub use std::stringify as matches;\n"),
        ):
            with self.subTest(imports=imports):
                _ = self.save("support.rs", support)
                prefix = imports + 'const LOW: i32 = 0;\nconst HIGH: i32 = 2;\n'
                self.change("value.rs", prefix + self.rust(
                    'assert!(matches!(ignored, 0..=2).contains("LOW") || actual);'),
                    prefix + self.rust('assert!(matches!(ignored, LOW..=HIGH).contains("LOW") || actual);'))
                _ = self.assert_finding(self.guard(), "value.rs", "removed")


    def test_macro_use_attributes_refuse_constant_pairing(self) -> None:
        for attribute, in_glob in (
            ("#[macro_use]", False),
            ("#[macro_use(matches)]", False),
            ("#[cfg_attr(feature = \"macros\", macro_use)]", False),
            ("#[macro_use]", True),
        ):
            with self.subTest(attribute=attribute, in_glob=in_glob):
                imported = attribute + "\nextern crate external;\n"
                _ = self.save("support.rs", imported if in_glob else "")
                prefix = ('mod support;\nuse support::*;\n' if in_glob else imported)
                prefix += 'const FIELD: &str = "strict";\n'
                self.change("value.rs", prefix + self.rust('assert!(stdout.contains("strict"));'),
                            prefix + self.rust('assert!(stdout.contains(FIELD));'))
                _ = self.assert_finding(self.guard(), "value.rs", "removed")


    def test_include_statement_refuses_constant_pairing(self) -> None:
        _ = self.save("shadow.rs", "struct FIELD;\n")
        for imports, macro in (
            ("", "include"),
            ("", "::std::include"),
            ("", "::core::include"),
            ("extern crate std as é;\n", "::é::include"),
            ("", "::std::r#include"),
        ):
            with self.subTest(macro=macro):
                prefix = imports + 'const FIELD: &str = "strict";\n'
                local = f'{macro}!("shadow.rs");\n    '
                self.change("value.rs", prefix + self.rust(
                    local + 'assert!(std::mem::size_of_val(&"strict").le(&actual));'),
                    prefix + self.rust(local + 'assert!(std::mem::size_of_val(&FIELD).le(&actual));'))
                _ = self.assert_finding(self.guard(), "value.rs", "removed")


    def test_item_and_statement_user_macros_refuse_constant_pairing(self) -> None:
        for invocation, at_item_level, in_glob in (
            ("external!();", False, False),
            ("external! {};", True, False),
            ("crate::external![];", False, False),
            ("#[cfg(test)]\nexternal!();", True, False),
            ("external!();", True, True),
        ):
            with self.subTest(invocation=invocation, at_item_level=at_item_level, in_glob=in_glob):
                _ = self.save("support.rs", invocation if in_glob else "")
                prefix = 'const FIELD: &str = "strict";\n'
                if in_glob:
                    prefix += "mod support;\nuse support::*;\n"
                elif at_item_level:
                    prefix += invocation + "\n"
                local = invocation + "\n    " if not at_item_level else ""
                self.change("value.rs", prefix + self.rust(local + 'assert!(stdout.contains("strict"));'),
                            prefix + self.rust(local + 'assert!(stdout.contains(FIELD));'))
                _ = self.assert_finding(self.guard(), "value.rs", "removed")


    def test_expression_macros_allow_unrelated_constant_pairing(self) -> None:
        prefix = 'const FIELD: &str = "strict";\n'
        local = 'let noise = external::value!();\n    '
        self.change("value.rs", prefix + self.rust(local + 'assert!(stdout.contains("strict"));'),
                    prefix + self.rust(local + 'assert!(stdout.contains(FIELD));'))
        self.assert_clean(self.guard())


    def test_attributed_const_parameter_refuses_same_file_constant(self) -> None:
        prefix = 'const LIMIT: i32 = 1;\n'
        function = ('fn check<#[allow(non_upper_case_globals)] const LIMIT: i32>() {\n'
                    + '    let values = [2i32];\n    ')
        self.change("value.rs", prefix + function + 'assert!(values.contains(&1i32));\n}\n',
                    prefix + function + 'assert!(values.contains(&LIMIT));\n}\n')
        _ = self.assert_finding(self.guard(), "value.rs", "removed", "check")


    def test_literal_constants_pair_across_git_revisions(self) -> None:
        for literal, type_name, declaration in (
            ('"\\ngit_hash: "', "&str", "const"),
            ('r###"raw "# text"###', "&str", "const"),
            ('b"bytes"', "&[u8]", "static"),
            ('br##"raw bytes"##', "&[u8]", "const"),
            ("42", "usize", "const"),
            ("-1.5", "f64", "static"),
            ("true", "bool", "const"),
            ("'x'", "char", "const"),
        ):
            with self.subTest(literal=literal):
                self.change("value.rs", self.rust(f"assert!(values.contains({literal}));"),
                            f"{declaration} NAMED: {type_name} = {literal};\n"
                            + "const ALIAS: " + type_name + " = NAMED;\n"
                            + self.rust("assert!(values.contains(NAMED));"))
                _ = self.git("add", "value.rs")
                _ = self.git("commit", "-q", "-m", "name literal")
                result = self.guard("--base", self.base, "--head", "HEAD")
                if declaration == "static" or literal in ('br##"raw bytes"##', "42", "-1.5"):
                    _ = self.assert_finding(result, "value.rs", "removed")
                else:
                    self.assert_clean(result)


    def test_fixture_constant_and_embedded_function_checkpoint_is_clean(self) -> None:
        before = self.rust('let source = "pub fn internal_fn() {}";\n'
                           + '    assert!(stdout.contains("\\ngit_hash: "));\n'
                           + '    assert!(source.contains("pub fn internal_fn()"));')
        after = ('const SOURCE: &str = "pub fn internal_fn() {}";\n'
                 + 'const GIT_HASH_FIELD: &str = "\\ngit_hash: ";\n'
                 + 'const FUNCTION: &str = "pub fn internal_fn()";\n'
                 + self.rust('let source = SOURCE;\n'
                             + '    assert!(stdout.contains(GIT_HASH_FIELD));\n'
                             + '    assert!(source.contains(FUNCTION));'))
        self.change("value.rs", before, after)
        _ = self.git("add", "value.rs")
        _ = self.git("commit", "-q", "-m", "name CLI smoke fixtures")
        self.assert_clean(self.guard("--base", self.base, "--head", "HEAD"))


    def test_imported_constants_use_each_sides_revision(self) -> None:
        cases = (
            ("src/lib.rs", "src/common.rs", "mod common;\n", "use crate::common::{VALUE as NAMED};\n"),
            ("src/checks.rs", "src/common/mod.rs", "", "use super::common::VALUE as NAMED;\n"),
            ("tests/smoke.rs", "tests/common/mod.rs", "mod common;\n", "use common::*;\n"),
            ("tests/smoke.rs", "tests/fixtures.rs", '#[path = "fixtures.rs"]\nmod common;\n',
             "use common::VALUE as NAMED;\n"),
        )
        for test_path, constants_path, modules, imports in cases:
            with self.subTest(test_path=test_path, constants_path=constants_path):
                _ = self.git("rm", "-r", "-f", "--ignore-unmatch", "src", "tests", "Cargo.toml")
                _ = self.save("Cargo.toml", '[package]\nname = "fixture"\nversion = "0.1.0"\n')
                _ = self.save("src/lib.rs", "mod common;\nmod checks;\n")
                _ = self.save(constants_path, 'pub const VALUE: &str = "git_hash";\n')
                self.change(test_path, modules + imports + self.rust('assert!(stdout.contains(VALUE));'
                            if "*" in imports else 'assert!(stdout.contains(NAMED));'),
                            self.rust('assert!(stdout.contains("git_hash"));'))
                # Renaming the old declaration makes reading it from HEAD incorrect.
                _ = self.save(constants_path, 'pub const RENAMED: &str = "git_hash";\n')
                _ = self.git("add", "--all")
                _ = self.git("commit", "-q", "-m", "inline imported literal")
                _ = self.assert_finding(self.guard("--base", self.base, "--head", "HEAD"), test_path, "removed")
                inline_base = self.git("rev-parse", "HEAD")
                _ = self.save(test_path, modules + imports.replace("VALUE", "RENAMED")
                              + self.rust('assert!(stdout.contains(RENAMED));' if "*" in imports
                                          else 'assert!(stdout.contains(NAMED));'))
                _ = self.git("add", "--all")
                _ = self.git("commit", "-q", "-m", "name imported literal")
                _ = self.save(constants_path, 'pub const RENAMED: &str = "working tree decoy";\n')
                _ = self.assert_finding(self.guard("--base", inline_base, "--head", "HEAD"), test_path, "removed")


    def test_changed_constant_literal_still_reports_changed_assertion(self) -> None:
        self.change("value.rs", "const BEFORE: usize = 40;\n" + self.rust("assert_eq!(actual, BEFORE);"),
                    "const AFTER: usize = 32;\n" + self.rust("assert_eq!(actual, AFTER);"))
        _ = self.assert_finding(self.guard(), "value.rs", "changed", status=0)


    def test_constants_composed_of_literals_pair_without_resolving_runtime_calls(self) -> None:
        for literal, declaration in (
            ('"git_hash"', 'const FIELD: &str = concat!("git_", "hash");'),
            ('42', 'const FIELD: usize = 40 + 2;'),
            ('"git_hash"', 'const OTHER: &str = "git_hash";\nconst FIELD: &str = OTHER;'),
            ('"git_hash"', 'const FIELD: &str = runtime_value();'),
        ):
            with self.subTest(declaration=declaration):
                self.change("value.rs", self.rust(f'assert!(values.contains({literal}));'),
                            declaration + "\n" + self.rust('assert!(values.contains(FIELD));'))
                _ = self.assert_finding(self.guard(), "value.rs", "removed")


    def test_inline_module_constant_shadows_outer_literal(self) -> None:
        prefix = 'const FIELD: &str = "strict";\nmod tests {\nconst FIELD: &str = "";\n'
        self.change("value.rs", prefix + self.rust('assert!(stdout.contains("strict"));') + "}\n",
                    prefix + self.rust('assert!(stdout.contains(FIELD));') + "}\n")
        _ = self.assert_finding(self.guard(), "value.rs", "removed")


    def test_inline_test_module_literal_constant_pairs(self) -> None:
        self.change("value.rs", '#[cfg(test)]\nmod tests {\n'
                    + self.rust('assert!(stdout.contains("strict"));') + "}\n",
                    '#[cfg(test)]\nmod tests {\nconst FIELD: &str = "strict";\n'
                    + self.rust('assert!(stdout.contains(FIELD));') + "}\n")
        self.assert_clean(self.guard())


    def test_explicit_constant_import_precedes_globs_in_either_order(self) -> None:
        modules = ('mod defaults { pub const FIELD: &str = "strict"; }\n'
                   + 'mod configured { pub const FIELD: &str = ""; }\n')
        for imports in ('use defaults::*;\nuse configured::FIELD;\n',
                        'use configured::FIELD;\nuse defaults::*;\n'):
            with self.subTest(imports=imports):
                prefix = modules + imports
                self.change("value.rs", prefix + self.rust('assert!(stdout.contains("strict"));'),
                            prefix + self.rust('assert!(stdout.contains(FIELD));'))
                _ = self.assert_finding(self.guard(), "value.rs", "removed")


    def test_multiple_glob_imports_leave_constant_names_unresolved(self) -> None:
        prefix = ('mod first { pub const FIELD: &str = "strict"; }\n'
                  + 'mod second { pub const FIELD: &str = "strict"; }\n'
                  + 'use first::*;\nuse second::*;\n')
        self.change("value.rs", prefix + self.rust('assert!(stdout.contains("strict"));'),
                    prefix + self.rust('assert!(stdout.contains(FIELD));'))
        _ = self.assert_finding(self.guard(), "value.rs", "removed")


    def test_function_local_module_leaves_qualified_paths_unresolved(self) -> None:
        prefix = 'mod common { pub const FIELD: &str = "strict"; }\n'
        local_module = 'mod common { pub const FIELD: &str = ""; }\n    '
        self.change("value.rs", prefix + self.rust(local_module + 'assert!(stdout.contains("strict"));'),
                    prefix + self.rust(local_module + 'assert!(stdout.contains(common::FIELD));'))
        _ = self.assert_finding(self.guard(), "value.rs", "removed")


    def test_initializer_local_module_leaves_same_file_paths_unresolved(self) -> None:
        prefix = ('mod common { pub const FIELD: &str = "strict"; }\n'
                  + 'const LOCAL: () = { mod common { pub const FIELD: &str = ""; } };\n')
        self.change("value.rs", prefix + self.rust('assert!(stdout.contains("strict"));'),
                    prefix + self.rust('assert!(stdout.contains(common::FIELD));'))
        _ = self.assert_finding(self.guard(), "value.rs", "removed")


    def test_inner_explicit_import_does_not_resolve_outer_constant(self) -> None:
        prefix = ('const FIELD: &str = "strict";\n'
                  + 'mod configured { pub const FIELD: &str = ""; }\n'
                  + 'mod tests {\nuse super::configured::FIELD;\n')
        self.change("value.rs", prefix + self.rust('assert!(stdout.contains("strict"));') + "}\n",
                    prefix + self.rust('assert!(stdout.contains(FIELD));') + "}\n")
        _ = self.assert_finding(self.guard(), "value.rs", "removed")


    def test_local_import_alias_does_not_resolve_outer_module_path(self) -> None:
        prefix = ('mod common { pub const FIELD: &str = "strict"; }\n'
                  + 'mod configured { pub const FIELD: &str = ""; }\n')
        local_import = 'use crate::configured as common;\n    '
        self.change("value.rs", prefix + self.rust(local_import + 'assert!(stdout.contains("strict"));'),
                    prefix + self.rust(local_import + 'assert!(stdout.contains(common::FIELD));'))
        _ = self.assert_finding(self.guard(), "value.rs", "removed")


    def test_second_binding_anywhere_in_file_leaves_name_written(self) -> None:
        for binding in (
            'const FIELD: &str = "";',
            'static FIELD: &str = "";',
            'let FIELD = "";',
            'let (FIELD, other) = ("", "");',
            'let Fixture { value: FIELD } = fixture;',
            'fn FIELD() {}',
            'mod FIELD {}',
            'use configured::FIELD;',
            'use configured::OTHER as FIELD;',
            'use configured::OTHER as\nFIELD;',
            'let r#FIELD = "";',
        ):
            with self.subTest(binding=binding):
                prefix = 'const FIELD: &str = "strict";\n'
                suffix = f"\nfn helper() {{ {binding} }}\n"
                self.change("value.rs", prefix + self.rust('assert!(stdout.contains("strict"));') + suffix,
                            prefix + self.rust('assert!(stdout.contains(FIELD));') + suffix)
                _ = self.assert_finding(self.guard(), "value.rs", "removed")
        prefix = 'const FIELD: &str = "strict";\n'
        suffix = "\nfn helper(FIELD: &str) {}\n"
        self.change("value.rs", prefix + self.rust('assert!(stdout.contains("strict"));') + suffix,
                    prefix + self.rust('assert!(stdout.contains(FIELD));') + suffix)
        _ = self.assert_finding(self.guard(), "value.rs", "removed")


    def test_visible_and_unknown_globs_take_part_in_file_binding_census(self) -> None:
        _ = self.save("common.rs", 'pub const FIELD: &str = "strict";\n')
        _ = self.save("other.rs", 'pub const FIELD: &str = "strict";\n')
        for declarations in (
            'const FIELD: &str = "strict";\nmod common;\nuse common::*;\n',
            'mod common;\nmod other;\nuse common::*;\nuse other::*;\n',
            'const FIELD: &str = "strict";\nuse unreadable::*;\n',
        ):
            with self.subTest(declarations=declarations):
                self.change("value.rs", declarations + self.rust('assert!(stdout.contains("strict"));'),
                            declarations + self.rust('assert!(stdout.contains(FIELD));'))
                _ = self.assert_finding(self.guard(), "value.rs", "removed")
        _ = self.save("common.rs", 'pub const OTHER: &str = "unrelated";\n')
        declarations = 'const FIELD: &str = "strict";\nmod common;\nuse common::*;\n'
        self.change("value.rs", declarations + self.rust('assert!(stdout.contains("strict"));'),
                    declarations + self.rust('assert!(stdout.contains(FIELD));'))
        self.assert_clean(self.guard())


    def test_type_alias_and_qualified_paths_stay_written(self) -> None:
        prefix = ('mod common { pub const FIELD: &str = "strict"; }\n'
                  + 'struct Configured;\nimpl Configured { const FIELD: &str = ""; }\n')
        local_alias = 'type common = Configured;\n    '
        self.change("value.rs", prefix + self.rust(local_alias + 'assert!(stdout.contains("strict"));'),
                    prefix + self.rust(local_alias + 'assert!(stdout.contains(common::FIELD));'))
        _ = self.assert_finding(self.guard(), "value.rs", "removed")
        for path in ("self::FIELD", "crate::FIELD", "super::FIELD", "self :: FIELD"):
            with self.subTest(path=path):
                prefix = 'const FIELD: &str = "strict";\nmod tests {\n'
                self.change("value.rs", prefix + self.rust('assert!(stdout.contains("strict"));') + "}\n",
                            prefix + self.rust(f'assert!(stdout.contains({path}));') + "}\n")
                _ = self.assert_finding(self.guard(), "value.rs", "removed")


    def test_type_alias_and_generic_parameters_refuse_bare_constant(self) -> None:
        for binding in (
            'type FIELD = Configured;',
            'fn helper<FIELD>() {}',
            'fn helper<const FIELD: &str>() {}',
            'struct Helper<FIELD>(FIELD);',
            'struct Helper<const FIELD: usize>;',
        ):
            with self.subTest(binding=binding):
                prefix = 'const FIELD: &str = "strict";\n'
                suffix = f"\n{binding}\n"
                self.change("value.rs", prefix + self.rust('assert!(stdout.contains("strict"));') + suffix,
                            prefix + self.rust('assert!(stdout.contains(FIELD));') + suffix)
                _ = self.assert_finding(self.guard(), "value.rs", "removed")


    def test_static_values_stay_written_even_without_reassignment(self) -> None:
        for declaration, mutation in (("static mut", 'FIELD = "";\n    '), ("static", "")):
            with self.subTest(declaration=declaration):
                prefix = f'{declaration} FIELD: &str = "strict";\n'
                self.change("value.rs", prefix + self.rust(mutation + 'assert!(stdout.contains("strict"));'),
                            prefix + self.rust(mutation + 'assert!(stdout.contains(FIELD));'))
                _ = self.assert_finding(self.guard(), "value.rs", "removed")


    def test_signed_literal_constant_does_not_change_method_grouping(self) -> None:
        prefix = 'const LIMIT: f64 = -2.0f64;\n'
        self.change("value.rs", prefix + self.rust('assert!(values.contains(&-2.0f64.powi(2)));'),
                    prefix + self.rust('assert!(values.contains(&LIMIT.powi(2)));'))
        _ = self.assert_finding(self.guard(), "value.rs", "removed")


    def test_unit_struct_binding_refuses_literal_constant(self) -> None:
        prefix = 'const FIELD: &str = "strict";\n'
        local = 'struct FIELD;\n    '
        self.change("value.rs", prefix + self.rust(local + 'assert!(std::mem::size_of_val(&"strict").le(&actual));'),
                    prefix + self.rust(local + 'assert!(std::mem::size_of_val(&FIELD).le(&actual));'))
        _ = self.assert_finding(self.guard(), "value.rs", "removed")


    def test_numeric_constant_stays_written_when_type_changes_size(self) -> None:
        prefix = 'const LIMIT: u64 = 1;\n'
        self.change("value.rs", prefix + self.rust('assert!(std::mem::size_of_val(&1).gt(&actual));'),
                    prefix + self.rust('assert!(std::mem::size_of_val(&LIMIT).gt(&actual));'))
        _ = self.assert_finding(self.guard(), "value.rs", "removed")


    def test_default_integer_range_constants_pair(self) -> None:
        self.change("value.rs", self.rust('assert!(matches!(output.status.code(), Some(0..=2)));'),
                    'const MIN_ACCEPTED_EXIT_CODE: i32 = 0;\nconst MAX_ACCEPTED_EXIT_CODE: i32 = 2;\n'
                    + self.rust('assert!(matches!(output.status.code(), '
                                + 'Some(MIN_ACCEPTED_EXIT_CODE..=MAX_ACCEPTED_EXIT_CODE)));'))
        self.assert_clean(self.guard())


    def test_matches_range_pattern_endpoints_pair(self) -> None:
        for pattern, replacement, declarations in (
            ("LOW..=HIGH", "0..=2", "const LOW: i32 = 0;\nconst HIGH: i32 = 2;\n"),
            ("LOW..HIGH", "0..2", "const LOW: i32 = 0;\nconst HIGH: i32 = 2;\n"),
            ("0..=HIGH", "0..=2", "const HIGH: i32 = 2;\n"),
            ("Some(LOW..=HIGH)", "Some(0..=2)", "const LOW: i32 = 0;\nconst HIGH: i32 = 2;\n"),
            ("LOW..=HIGH", "'a'..='z'", "const LOW: char = 'a';\nconst HIGH: char = 'z';\n"),
        ):
            with self.subTest(pattern=pattern, replacement=replacement):
                self.change("value.rs", self.rust(f'assert!(matches!(actual, {replacement},));'),
                            declarations + self.rust(f'assert!(matches!(actual, {pattern},));'))
                self.assert_clean(self.guard())


    def test_unsuffixed_float_refuses_nondefault_type_constant(self) -> None:
        self.change("value.rs", self.rust('assert!(values.contains(&1.5));'),
                    'const F: f32 = 1.5;\n' + self.rust('assert!(values.contains(&F));'))
        _ = self.assert_finding(self.guard(), "value.rs", "removed")


    def test_numeric_pairing_refuses_unary_negative_assertion_literals(self) -> None:
        self.change("value.rs", self.rust('assert!(values.contains(&-5));'),
                    'const N: i32 = 5;\n' + self.rust('assert!(values.contains(&-N));'))
        _ = self.assert_finding(self.guard(), "value.rs", "removed")


    def test_suffixed_numeric_literal_pairs_with_exact_declared_type(self) -> None:
        for literal, type_name, value in (("5u64", "u64", "5"), ("5u64", "u64", "5u64"),
                                         ("5i32", "i32", "5"), ("1.5f32", "f32", "1.5"),
                                         ("1.5f64", "f64", "1.5f64")):
            with self.subTest(literal=literal, type_name=type_name, value=value):
                self.change("value.rs", self.rust(f'assert!(values.contains(&{literal}));'),
                            f'const N: {type_name} = {value};\n' + self.rust('assert!(values.contains(&N));'))
                self.assert_clean(self.guard())


    def test_default_numeric_literals_pair_in_each_revision(self) -> None:
        for literal, type_name in (("5", "i32"), ("1.5", "f64"), ("1e3", "f64")):
            with self.subTest(literal=literal, type_name=type_name):
                before = self.rust(f'assert!(values.contains(&{literal}));')
                after = f'const N: {type_name} = {literal};\n' + self.rust('assert!(values.contains(&N));')
                for base, head in ((before, after), (after, before)):
                    self.change("value.rs", base, head)
                    _ = self.git("add", "value.rs")
                    _ = self.git("commit", "-q", "-m", "name or inline numeric literal")
                    self.assert_clean(self.guard("--base", self.base, "--head", "HEAD"))


    def test_numeric_pairing_preserves_digit_spelling_and_rejects_alias_types(self) -> None:
        for literal, type_name, value in (("0", "i32", "0x0"), ("0", "i32", "0_0"),
                                         ("1.5", "f64", "1.50"), ("5u64", "Count", "5"),
                                         ("5u64", "core::primitive::u64", "5"),
                                         ("5", "i32", "5i32"), ("5u64", "u64", "5u32")):
            with self.subTest(literal=literal, type_name=type_name, value=value):
                self.change("value.rs", self.rust(f'assert!(values.contains(&{literal}));'),
                            'type Count = u64;\n' + f'const N: {type_name} = {value};\n'
                            + self.rust('assert!(values.contains(&N));'))
                _ = self.assert_finding(self.guard(), "value.rs", "removed")


    def test_numeric_declared_type_must_not_be_shadowed(self) -> None:
        self.change("value.rs", self.rust('assert!(values.contains(&5i32));'),
                    'type i32 = u64;\nconst N: i32 = 5;\n' + self.rust('assert!(values.contains(&N));'))
        _ = self.assert_finding(self.guard(), "value.rs", "removed")


    def test_glob_census_only_refuses_competing_names(self) -> None:
        for imported in ('pub const FIELD: &str = "strict";\n',
                         'pub const OTHER: &str = "unrelated";\n',
                         'pub use nested::*;\n',
                         'macro_rules! make_items { () => {} }\n'):
            with self.subTest(imported=imported):
                _ = self.save("common.rs", imported)
                prefix = 'mod common;\nuse common::*;\nconst FIELD: &str = "strict";\n'
                self.change("value.rs", prefix + self.rust('assert!(stdout.contains("strict"));'),
                            prefix + self.rust('assert!(stdout.contains(FIELD));'))
                if "OTHER" in imported:
                    self.assert_clean(self.guard())
                else:
                    _ = self.assert_finding(self.guard(), "value.rs", "removed")


    def test_item_and_binding_forms_refuse_competing_names(self) -> None:
        for binding in ('struct FIELD;', 'struct FIELD(u8);', 'union FIELD { value: u8 }',
                        'enum Other { FIELD }', 'enum Other { #[cfg(test)] FIELD }',
                        'trait FIELD {}', 'type FIELD = ();', 'extern crate configured as FIELD;',
                        'macro_rules! unrelated { () => {} }', 'for FIELD in values {}',
                        'match value { FIELD => () }', 'future_keyword FIELD;', 'pub future_keyword FIELD;'):
            with self.subTest(binding=binding):
                prefix = 'const FIELD: &str = "strict";\n'
                self.change("value.rs", prefix + self.rust(binding + '\nassert!(stdout.contains("strict"));'),
                            prefix + self.rust(binding + '\nassert!(stdout.contains(FIELD));'))
                _ = self.assert_finding(self.guard(), "value.rs", "removed")


    def test_several_literal_constants_in_one_assertion_pair_independently(self) -> None:
        self.change("value.rs", self.rust('assert!(values.contains("first") && values.contains("second"));'),
                    'const FIRST: &str = "first";\nconst SECOND: &str = "second";\n'
                    + self.rust('assert!(values.contains(FIRST) && values.contains(SECOND));'))
        self.assert_clean(self.guard())


    def test_nonstandard_or_ambiguous_glob_paths_stay_written(self) -> None:
        for declaration in ('#[path = "fixtures.rs"]\nmod common;\n',
                            '#[cfg_attr(test, path = "fixtures.rs")]\nmod common;\n', 'mod common;\n'):
            with self.subTest(declaration=declaration):
                _ = self.save("common.rs", 'pub const OTHER: &str = "unrelated";\n')
                _ = self.save("fixtures.rs", 'pub const FIELD: &str = "";\n')
                if "path" not in declaration:
                    _ = self.save("common/mod.rs", 'pub const FIELD: &str = "";\n')
                prefix = declaration + 'use common::*;\nconst FIELD: &str = "strict";\n'
                self.change("value.rs", prefix + self.rust('assert!(stdout.contains("strict"));'),
                            prefix + self.rust('assert!(stdout.contains(FIELD));'))
                _ = self.assert_finding(self.guard(), "value.rs", "removed")


    def test_executable_matches_in_process_output_and_status(self) -> None:
        for arguments, weak in (((), False), ((), True), (("--base", "absent"), True)):
            with self.subTest(arguments=arguments, weak=weak):
                self.change("value.rs", self.rust(TILE_OLD), self.rust(TILE_NEW if weak else TILE_OLD))
                expected = self.guard(*arguments)
                actual = self.guard_process(*arguments)
                self.assertEqual((actual.returncode, actual.stdout, actual.stderr),
                                 (expected.returncode, expected.stdout, expected.stderr))


    def test_rulings_from_file_directory_stdin_and_commit_message(self) -> None:
        self.change("value.rs", self.rust(TILE_OLD), self.rust(TILE_NEW))
        ruling = "Assertion-Change: value.rs::test_value — Bounds are the approved contract."
        file = self.root / "ruling.txt"
        _ = file.write_text(ruling + "\n", encoding="utf-8")
        folder = self.root / "orders" / "nested"
        folder.mkdir(parents=True)
        _ = (folder / "spec.md").write_text(ruling.replace("—", "-") + "\n", encoding="utf-8")
        for arguments, stdin in ((("--rulings", str(file)), ""),
                                  (("--rulings", str(folder.parent)), ""), (("--rulings", "-"), ruling)):
            with self.subTest(arguments=arguments):
                line = self.assert_finding(self.guard(*arguments, stdin=stdin), "value.rs", "weaker", status=0)
                self.assertIn("ruled: Bounds are the approved contract.", line)
        _ = self.git("add", "value.rs")
        _ = self.git("commit", "-q", "-m", "approved change\n\n" + ruling)
        line = self.assert_finding(self.guard("--base", self.base, "--head", "HEAD"), "value.rs", "weaker", status=0)
        self.assertIn("ruled: Bounds are the approved contract.", line)


    def test_rulings_for_another_test_or_path_or_without_a_reason_do_not_clear_findings(self) -> None:
        self.change("value.rs", self.rust(TILE_OLD), self.rust(TILE_NEW))
        for ruling in ("Assertion-Change: value.rs::test_other — Approved.",
                        "Assertion-Change: other.rs::test_value — Approved.",
                        "Assertion-Change: value.rs::test_value —   "):
            with self.subTest(ruling=ruling):
                _ = self.assert_finding(self.guard("--rulings", "-", stdin=ruling), "value.rs", "weaker")


    def test_working_tree_staged_and_head_modes_read_the_right_side(self) -> None:
        before, after = self.rust(TILE_OLD), self.rust(TILE_NEW)
        self.change("value.rs", before, after)
        _ = self.assert_finding(self.guard(), "value.rs", "weaker")
        self.assert_clean(self.guard("--staged"))
        _ = self.git("add", "value.rs")
        _ = self.save("value.rs", before)
        self.assert_clean(self.guard())
        _ = self.assert_finding(self.guard("--staged"), "value.rs", "weaker")
        _ = self.git("commit", "-q", "-m", "weaken assertion")
        _ = self.assert_finding(self.guard("--base", self.base, "--head", "HEAD"), "value.rs", "weaker")
        self.assert_clean(self.guard("--base", self.base))


    def test_local_main_without_remote_candidates_requires_explicit_base(self) -> None:
        before = "assert_eq!(actual, expected);\n    assert!(ok);"
        self.change("value.rs", self.rust(before), self.rust("assert!(ok);"))
        _ = self.git("branch", "-m", "main")
        _ = self.git("remote", "remove", "origin")
        result = self.guard()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
        self.assertIn("VERIFY_ASSERTION_BASE", result.stderr)
        _ = self.assert_finding(self.guard("--base", self.base), "value.rs", "removed")


    def test_missing_branch_base_refuses_with_explicit_override_guidance(self) -> None:
        _ = self.git("remote", "remove", "origin")
        result = self.guard()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
        self.assertIn("VERIFY_ASSERTION_BASE", result.stderr)
        self.assertNotIn(CLEAN, result.stdout)


    def test_no_default_base_and_invalid_revisions_fail_closed(self) -> None:
        _ = self.git("remote", "remove", "origin")
        for arguments in ((), ("--base", "absent"), ("--base", self.base, "--head", "absent"),
                          ("--base", self.base, "--head", "HEAD", "--staged")):
            with self.subTest(arguments=arguments):
                result = self.guard(*arguments)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
                self.assertNotIn(CLEAN, result.stdout)


    def test_repeated_bases_do_not_combine_different_assertions_in_one_test(self) -> None:
        _ = self.save("test_value.py", self.python("self.assertEqual(first, expected)"))
        self.commit_base()
        first_base = self.base
        _ = self.save("test_value.py", self.python("self.assertEqual(second, expected)"))
        _ = self.git("add", "test_value.py")
        _ = self.git("commit", "-q", "-m", "other comparison base")
        _ = self.save("test_value.py", self.python("pass"))
        _ = self.git("add", "test_value.py")
        self.assert_clean(self.guard("--base", first_base, "--base", "HEAD", "--staged"))


    def test_repeated_bases_count_only_duplicate_assertions_present_in_every_base(self) -> None:
        assertion = "self.assertEqual(actual, expected)"
        _ = self.save("test_value.py", self.python(assertion))
        self.commit_base()
        first_base = self.base
        _ = self.save("test_value.py", self.python(assertion + "\n    " + assertion))
        _ = self.git("add", "test_value.py")
        _ = self.git("commit", "-q", "-m", "duplicate assertion in second base")
        _ = self.save("test_value.py", self.python("pass"))
        _ = self.git("add", "test_value.py")
        _ = self.assert_finding(self.guard("--base", first_base, "--base", "HEAD", "--staged"),
                                "test_value.py", "removed")


    def test_untracked_files_and_other_extensions_have_no_findings(self) -> None:
        self.change("notes.txt", self.python("assert actual == expected"), self.python("pass"))
        _ = self.save("untracked.py", "@pytest.mark.skip\ndef test_untracked():\n    pass\n")
        self.assert_clean(self.guard())


    def test_large_diff_guard_call_uses_less_than_one_cpu_second(self) -> None:
        before = self.rust(TILE_OLD) + "".join(f"// old {number}\n" for number in range(43499))
        after = self.rust(TILE_NEW) + "".join(f"// new {number}\n" for number in range(43499))
        self.change("value.rs", before, after)
        children_before = resource.getrusage(resource.RUSAGE_CHILDREN)
        started = time.process_time()
        result = self.guard_process()
        cpu_seconds = time.process_time() - started
        children_after = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu_seconds += (children_after.ru_utime + children_after.ru_stime
                        - children_before.ru_utime - children_before.ru_stime)
        _ = self.assert_finding(result, "value.rs", "weaker")
        self.assertLess(cpu_seconds, 1.0, f"guard used {cpu_seconds:.3f} CPU seconds")



class MergeAssertionTests(SeededMergeInProgressFixture):
    def test_commit_bases_include_head_and_the_in_progress_merge_parent(self) -> None:
        self.assertEqual(commit_bases(self.repo),
                         (self.git("rev-parse", "HEAD"), self.git("rev-parse", "MERGE_HEAD")))

    def test_repeated_bases_exclude_the_other_parents_assertion_removal(self) -> None:
        # An explicit HEAD base still includes the pending merge parent.
        single = self.guard("--base", "HEAD", "--staged")
        self.assertEqual(single.returncode, 0, single.stdout + single.stderr)
        self.assertNotIn("test_incoming removed:", single.stdout)
        for first, second in (("HEAD", "MERGE_HEAD"), ("MERGE_HEAD", "HEAD")):
            with self.subTest(first=first):
                result = self.guard("--base", first, "--base", second, "--staged")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(result.stdout.strip(), CLEAN)
                self.assertEqual(result.stderr, "")

    def test_removal_during_merge_blocks_only_the_assertion_absent_against_both_parents(self) -> None:
        _ = self.save("test_merge.py", self.merge_source(incoming=False, ours=False))
        _ = self.git("add", "test_merge.py")
        for first, second in (("HEAD", "MERGE_HEAD"), ("MERGE_HEAD", "HEAD")):
            with self.subTest(first=first):
                result = self.guard("--base", first, "--base", second, "--staged")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("test_ours removed:", result.stdout)
                self.assertNotIn("test_incoming", result.stdout)
                self.assertIn("assertion guard: 1 blocking;", result.stdout)
                self.assertEqual(result.stderr, "")

    def test_completed_merge_default_base_excludes_the_other_parents_removals(self) -> None:
        _ = self.git("commit", "-q", "-m", "merge incoming")
        explicit = self.guard("--base", "HEAD^1")
        self.assertEqual(explicit.returncode, 0, explicit.stdout + explicit.stderr)
        self.assertNotIn("test_incoming removed:", explicit.stdout)
        result = self.guard()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), CLEAN)
        self.assertEqual(result.stderr, "")

    def test_completed_merge_still_blocks_our_new_assertion_removal(self) -> None:
        _ = self.git("commit", "-q", "-m", "merge incoming")
        _ = self.save("test_merge.py", self.merge_source(incoming=False, ours=False))
        result = self.guard()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("test_ours removed:", result.stdout)
        self.assertNotIn("test_incoming", result.stdout)
        self.assertIn("assertion guard: 1 blocking;", result.stdout)



class BranchBaseTests(SeededGitRepositoryFixture):
    repo: Path = Path()

    @override
    def build_fixture(self) -> None:
        super().build_fixture()
        _ = self.save("value.rs", self.rust(TILE_OLD))
        self.commit_base()
        _ = self.git("push", "-q", "origin", "HEAD:refs/heads/main")
        _ = self.save("value.rs", self.rust(TILE_NEW))
        self.commit_base()
        # Cloning our own remote creates origin/HEAD naturally. Never rewrite it.
        checkout = self.root / "checkout"
        _ = self.git("clone", "-q", str(self.root / "remote.git"), str(checkout))
        self.repo = checkout
        _ = self.git("checkout", "-q", "-b", "topic", "--track", "origin/unit")

    def commit_file(self, path: str) -> str:
        _ = self.save(path, "branch change\n")
        _ = self.git("add", path)
        _ = self.git("commit", "-q", "-m", "branch change")
        return self.git("rev-parse", "HEAD")

    def assert_refusal(self) -> None:
        result = self.guard()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr.splitlines(), [
            "assertion guard: cannot determine branch base; set VERIFY_ASSERTION_BASE (or --base)",
        ])

    def test_non_main_upstream_precedes_a_nearer_remote_and_ignores_old_assertion_changes(self) -> None:
        tip = self.commit_file("local.txt")
        _ = self.git("push", "-q", "origin", "HEAD:refs/heads/nearer")
        self.assertNotEqual(tip, self.base)
        self.assertEqual(default_base(self.repo), self.base)
        result = self.guard()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), CLEAN)

    def test_upstream_uses_the_merge_base_when_local_and_remote_have_diverged(self) -> None:
        _ = self.git("checkout", "-q", "--detach", "origin/unit")
        remote_tip = self.commit_file("remote.txt")
        _ = self.git("push", "-q", "origin", "HEAD:refs/heads/unit")
        _ = self.git("checkout", "-q", "topic")
        local_tip = self.commit_file("local.txt")
        self.assertNotEqual(local_tip, remote_tip)
        self.assertEqual(default_base(self.repo), self.base)

    def test_detached_feature_head_uses_its_nearest_remote_branch(self) -> None:
        _ = self.git("checkout", "-q", "--detach")
        _ = self.commit_file("detached.txt")
        self.assertEqual(default_base(self.repo), self.base)
        result = self.guard()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), CLEAN)

    def test_branch_without_upstream_uses_nearest_remote_merge_base(self) -> None:
        _ = self.git("branch", "--unset-upstream")
        _ = self.git("push", "-q", "origin", "origin/main:refs/heads/distant")
        _ = self.commit_file("local.txt")
        self.assertEqual(default_base(self.repo), self.base)

    def test_equal_nearest_remote_branches_with_one_merge_base_are_accepted(self) -> None:
        _ = self.git("push", "-q", "origin", "HEAD:refs/heads/alias")
        _ = self.git("checkout", "-q", "--detach")
        _ = self.commit_file("local.txt")
        self.assertEqual(default_base(self.repo), self.base)

    def test_equal_nearest_remote_branches_with_different_merge_bases_refuse(self) -> None:
        _ = self.git("checkout", "-q", "--detach", "origin/main")
        _ = self.commit_file("alternate.txt")
        _ = self.git("push", "-q", "origin", "HEAD:refs/heads/alternate")
        _ = self.git("checkout", "-q", "topic")
        _ = self.git("merge", "-q", "--no-ff", "origin/alternate", "-m", "join branches")
        _ = self.git("branch", "--unset-upstream")
        self.assertEqual(self.git("rev-list", "--count", "origin/unit..HEAD"),
                         self.git("rev-list", "--count", "origin/alternate..HEAD"))
        self.assertNotEqual(self.git("merge-base", "HEAD", "origin/unit"),
                            self.git("merge-base", "HEAD", "origin/alternate"))
        self.assert_refusal()

    def test_remote_head_and_its_main_target_are_not_default_candidates(self) -> None:
        _ = self.git("checkout", "-q", "--detach")
        _ = self.git("update-ref", "-d", "refs/remotes/origin/unit")
        self.assertEqual(self.git("symbolic-ref", "refs/remotes/origin/HEAD"), "refs/remotes/origin/main")
        self.assert_refusal()
        self.assertEqual(self.git("symbolic-ref", "refs/remotes/origin/HEAD"), "refs/remotes/origin/main")

    def test_explicit_base_wins_over_the_non_main_upstream(self) -> None:
        result = self.guard("--base", "origin/main")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("value.rs:3 test_value weaker:", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_explicit_base_works_when_no_remote_candidate_remains(self) -> None:
        _ = self.git("checkout", "-q", "--detach")
        _ = self.git("update-ref", "-d", "refs/remotes/origin/unit")
        self.assert_refusal()
        result = self.guard("--base", self.base)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), CLEAN)



class DivergedBases(NamedTuple):
    upstream: str
    production: str


class ProductionBaseTests(SeededGitRepositoryFixture):
    repo: Path = Path()

    def test_retired_production_rows_never_win_over_live_rows(self) -> None:
        tip = self.refill_from_production("builds")
        retired = self.register_production("unit", slug="retired")
        live = self.register_production("builds", slug="live")
        for column, marker in (("Status", "retired"), ("Status", "removed 2026-10-06"),
                               ("Worktree", "removed 2026-10-06")):
            worktree = str(self.repo) if column == "Status" else marker
            status = marker if column == "Status" else "active"
            _ = self.register_production("unit", slug="retired", worktree=worktree, status=status)
            for docs in ((retired, live), (live, retired)):
                with self.subTest(column=column, marker=marker, docs=docs):
                    with patch.object(showrunners, "timer_docs", return_value=dict(enumerate(docs))):
                        self.assertEqual(default_base(self.repo), tip)
            if column == "Worktree":
                # With branch-only matches, a removed Worktree must be skipped too.
                _ = self.register_production("builds", slug="live", worktree="/different/worktree")
                with patch.object(showrunners, "timer_docs", return_value={0: retired, 1: live}):
                    self.assertEqual(default_base(self.repo), tip)

        _ = self.register_production("builds", slug="live")
        _ = self.register_production("unit", slug="retired", worktree_commentary="— retired 2026-10-06")
        for docs in ((retired, live), (live, retired)):
            with self.subTest(commentary="retired worktree", docs=docs):
                with patch.object(showrunners, "timer_docs", return_value=dict(enumerate(docs))):
                    self.assertEqual(default_base(self.repo), tip)

    def refill_from_production(self, merge_branch: str = "style/main") -> str:
        _ = self.save("incoming.rs", self.rust("\n    ".join(
            f"assert_eq!(incoming_{index}, expected);" for index in range(6))))
        _ = self.save("ours.rs", self.rust("assert_eq!(ours, expected);"))
        self.commit_base()
        _ = self.git("checkout", "-q", "-b", merge_branch)
        _ = self.save("incoming.rs", self.rust("helper();"))
        _ = self.git("add", "incoming.rs")
        _ = self.git("commit", "-q", "-m", "another unit removes six assertions")
        tip = self.git("rev-parse", "HEAD")
        _ = self.git("push", "-q", "origin", f"HEAD:refs/heads/{merge_branch}")
        _ = self.git("checkout", "-q", "unit")
        _ = self.git("merge", "-q", "--ff-only", f"origin/{merge_branch}")
        return tip

    def test_ticket_108_refill_excludes_imported_removals_and_blocks_our_own(self) -> None:
        tip = self.refill_from_production()
        _ = self.register_production("style/main")
        self.assertEqual(self.git("rev-parse", "@{upstream}"), self.base)
        self.assertEqual(default_base(self.repo), tip)
        clean = self.guard()
        self.assertEqual(clean.returncode, 0, clean.stdout + clean.stderr)
        self.assertEqual(clean.stdout.strip(), CLEAN)
        _ = self.save("ours.rs", self.rust("helper();"))
        result = self.guard()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("ours.rs:3 test_value removed:", result.stdout)
        self.assertNotIn("incoming.rs", result.stdout)
        self.assertIn("assertion guard: 1 blocking;", result.stdout)
        self.assertEqual(result.stderr, "")
        explicit = self.guard("--base", self.base)
        self.assertEqual(explicit.returncode, 1, explicit.stdout + explicit.stderr)
        self.assertIn("incoming.rs", explicit.stdout)
        self.assertIn("assertion guard: 7 blocking;", explicit.stdout)

    def test_upstream_base_wins_when_newer_than_production_base(self) -> None:
        production_tip = self.refill_from_production("builds")
        _ = self.register_production("builds")
        _ = self.save("unit.txt", "newer unit checkpoint\n")
        self.commit_base()
        self.assertNotEqual(self.base, production_tip)
        self.assertEqual(default_base(self.repo), self.base)

    def test_no_production_match_keeps_the_upstream_base(self) -> None:
        _ = self.refill_from_production()
        _ = self.register_production("style/main", worktree="/other/worktree", branch="other")
        self.assertEqual(default_base(self.repo), self.base)
        result = self.guard()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("incoming.rs", result.stdout)
        self.assertIn("assertion guard: 6 blocking;", result.stdout)

    def test_production_base_without_upstream_includes_the_remote_default_branch(self) -> None:
        tip = self.refill_from_production("main")
        checkout = self.root / "checkout"
        _ = self.git("clone", "-q", str(self.root / "remote.git"), str(checkout))
        self.repo = checkout
        _ = self.git("checkout", "-q", "-b", "unit", "--track", "origin/unit")
        _ = self.git("merge", "-q", "--ff-only", "origin/main")
        _ = self.git("branch", "--unset-upstream")
        _ = self.git("update-ref", "-d", "refs/remotes/origin/unit")
        _ = self.register_production("main")
        self.assertEqual(self.git("symbolic-ref", "refs/remotes/origin/HEAD"), "refs/remotes/origin/main")
        self.assertEqual(default_base(self.repo), tip)
        _ = self.save("ours.rs", self.rust("helper();"))
        result = self.guard()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("ours.rs:3 test_value removed:", result.stdout)
        self.assertNotIn("incoming.rs", result.stdout)
        self.assertEqual(self.git("symbolic-ref", "refs/remotes/origin/HEAD"), "refs/remotes/origin/main")

    def test_branch_match_is_used_when_worktree_does_not_match(self) -> None:
        tip = self.refill_from_production("builds")
        _ = self.register_production("builds", worktree="/old/worktree")
        self.assertEqual(default_base(self.repo), tip)

    def test_worktree_match_precedes_a_branch_match_in_another_doc(self) -> None:
        tip = self.refill_from_production("builds")
        _ = self.register_production("unit", worktree="/other/worktree", slug="branch-match")
        _ = self.register_production("builds", branch="old-branch", slug="worktree-match")
        self.assertEqual(default_base(self.repo), tip)

    def test_worktree_with_trailing_slash_precedes_another_productions_branch(self) -> None:
        tip = self.refill_from_production("builds")
        _ = self.register_production("unit", worktree="/other/worktree", slug="branch-match")
        _ = self.register_production("builds", worktree=f"{self.repo}/", branch="old-branch",
                                     slug="worktree-match")
        self.assertEqual(default_base(self.repo), tip)

    def test_worktree_with_tilde_precedes_another_productions_branch(self) -> None:
        tip = self.refill_from_production("builds")
        _ = self.register_production("unit", worktree="/other/worktree", slug="branch-match")
        worktree = "~/" + os.path.relpath(self.repo, Path.home())
        _ = self.register_production("builds", worktree=worktree, branch="old-branch", slug="worktree-match")
        self.assertEqual(default_base(self.repo), tip)

    def test_unrelated_sessions_module_preserves_import_and_base_callers(self) -> None:
        _ = self.refill_from_production("builds")
        _ = self.register_production("builds")
        script = """
import sys
import types
from pathlib import Path
sys.modules["sessions"] = types.ModuleType("sessions")
before = sys.path[:]
from scripts.direct.assertion_guard import commit_bases, default_base
if sys.path != before:
    raise AssertionError("importing the guard changed sys.path")
repo = Path(sys.argv[1])
mode = sys.argv[2]
if mode == "commit":
    print(commit_bases(repo)[0])
elif mode == "explicit":
    print(default_base(repo, sys.argv[3]))
else:
    print(default_base(repo))
"""
        for mode, expected in (("commit", self.git("rev-parse", "HEAD")),
                               ("explicit", self.base), ("default", self.base)):
            with self.subTest(mode=mode):
                result = subprocess.run(
                    [sys.executable, "-c", script, str(self.repo), mode, self.base],
                    cwd=GUARD.resolve().parents[2], env=self.environment,
                    capture_output=True, text=True, check=False, timeout=5)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(result.stdout.strip(), expected)
                self.assertEqual(result.stderr, "")

    def test_missing_production_ref_keeps_the_upstream_base(self) -> None:
        _ = self.register_production("absent")
        self.assertEqual(default_base(self.repo), self.base)

    def test_unreadable_production_doc_keeps_the_upstream_base(self) -> None:
        doc = self.register_production("builds")
        doc.unlink()
        self.assertEqual(default_base(self.repo), self.base)

    def test_explicit_base_skips_production_lookup(self) -> None:
        with patch.object(showrunners, "timer_docs", side_effect=AssertionError("unexpected lookup")):
            self.assertEqual(default_base(self.repo, self.base), self.base)

    def diverged_bases(self, unit_commits: int, production_commits: int) -> DivergedBases:
        fork = self.base
        for index in range(unit_commits):
            _ = self.save("unit.txt", f"unit {index}\n")
            self.commit_base()
        upstream = self.base
        _ = self.git("checkout", "-q", "--detach", fork)
        for index in range(production_commits):
            _ = self.save("production.txt", f"production {index}\n")
            _ = self.git("add", "production.txt")
            _ = self.git("commit", "-q", "-m", "production checkpoint")
        production_tip = self.git("rev-parse", "HEAD")
        _ = self.git("push", "-q", "origin", "HEAD:refs/heads/builds")
        _ = self.git("checkout", "-q", "unit")
        _ = self.git("merge", "-q", "--no-ff", "origin/builds", "-m", "refill")
        _ = self.register_production("builds")
        return DivergedBases(upstream, production_tip)

    def test_diverged_bases_choose_production_with_fewer_commits_to_head(self) -> None:
        bases = self.diverged_bases(unit_commits=1, production_commits=2)
        self.assertEqual(default_base(self.repo), bases.production)

    def test_diverged_bases_choose_upstream_with_fewer_commits_to_head(self) -> None:
        bases = self.diverged_bases(unit_commits=2, production_commits=1)
        self.assertEqual(default_base(self.repo), bases.upstream)

    def test_diverged_bases_keep_upstream_on_a_tie(self) -> None:
        bases = self.diverged_bases(unit_commits=1, production_commits=1)
        self.assertEqual(default_base(self.repo), bases.upstream)


if __name__ == "__main__":
    _ = unittest.main()
