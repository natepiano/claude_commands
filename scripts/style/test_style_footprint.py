"""Exercise the mechanical-fix boundary through committed temporary Git diffs."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import final, override
from unittest import mock

import style_footprint


SCRIPT = Path(__file__).with_name("style_footprint.py")


@final
class StyleFootprintTests(unittest.TestCase):
    repo: Path = Path()

    @override
    def setUp(self) -> None:
        self.repo = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="style-footprint-")))
        _ = self.git("init", "-q", "--initial-branch=main")
        _ = self.git("config", "user.name", "Footprint tests")
        _ = self.git("config", "user.email", "footprint@example.invalid")
        _ = self.git("config", "commit.gpgsign", "false")
        self.write("Cargo.toml", '[package]\nname = "sample"\nversion = "0.1.0"\nedition = "2021"\n')

    def git(self, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), *arguments], check=True, capture_output=True, text=True,
        ).stdout.strip()

    def write(self, path: str, text: str) -> None:
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        _ = target.write_text(text)

    def commit(self) -> str:
        _ = self.git("add", "--all")
        _ = self.git("commit", "--allow-empty", "-qm", "Fixture change")
        return self.git("rev-parse", "HEAD")

    def check(self, base: str, head: str = "HEAD", *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "check", "--repo", str(self.repo), "--base", base, "--head", head, *extra],
            capture_output=True, text=True, check=False,
        )

    def assert_mechanical(self, base: str, head: str = "HEAD") -> None:
        result = self.check(base, head)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("mechanical", result.stdout)
        self.assertNotIn("review:", result.stdout)

    def assert_review(self, base: str, head: str = "HEAD") -> str:
        result = self.check(base, head)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("review:", result.stdout)
        return result.stdout

    def rename_fixture(self) -> str:
        self.write("src/lib.rs", "mod first;\nmod second;\nfn read_value(input: usize) -> usize { input }\n")
        self.write("src/first.rs", "fn first(input: usize) -> usize { super::read_value(input) }\n")
        self.write("src/second.rs", "fn second(input: usize) -> usize { super::read_value(input) }\n")
        return self.commit()

    def rename_value(self) -> str:
        for path in ("src/lib.rs", "src/first.rs", "src/second.rs"):
            self.write(path, (self.repo / path).read_text().replace("read_value", "load_value"))
        return self.commit()

    def worktree_check(self, base: str, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "check", "--repo", str(self.repo), "--base", base, "--worktree", *extra],
            capture_output=True, text=True, check=False,
        )

    def test_worktree_literal_change_is_judged_even_when_head_equals_base(self) -> None:
        self.write("src/lib.rs", "fn value() -> usize { 1 }\n")
        base = self.commit()
        _ = self.git("config", "core.trustctime", "false")
        source = self.repo / "src/lib.rs"
        before = source.stat()
        index = Path(self.git("rev-parse", "--path-format=absolute", "--git-path", "index"))
        os.utime(index, ns=(before.st_atime_ns, before.st_mtime_ns))
        index_before = index.read_bytes()
        self.write("src/lib.rs", "fn value() -> usize { 2 }\n")
        os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
        result = self.worktree_check(base)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("review: check 6:", result.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), base)
        self.assertEqual(index.read_bytes(), index_before)

    def test_worktree_rename_preserves_staged_paths_and_index_bytes_and_ignores_excluded_files(self) -> None:
        base = self.rename_fixture()
        index = Path(self.git("rev-parse", "--path-format=absolute", "--git-path", "index"))
        self.write(".git/info/exclude", "/docs/style-production/\n")
        self.write("docs/style-production/units/names-unit.md", "excluded plan\n")
        self.write("docs/style-production/rules/blob.md", "excluded pin\n")
        for path in ("src/lib.rs", "src/first.rs", "src/second.rs"):
            self.write(path, (self.repo / path).read_text().replace("read_value", "load_value"))
        _ = self.git("add", "src/lib.rs")
        before = index.read_bytes(), self.git("diff", "--cached", "--name-only")
        result = self.worktree_check(base)
        self.assertEqual((result.returncode, result.stdout), (0, "mechanical\n"), result.stderr)
        self.assertEqual((index.read_bytes(), self.git("diff", "--cached", "--name-only")), before)

    def test_worktree_new_untracked_file_counts_and_failure_preserves_the_real_index(self) -> None:
        self.write("src/lib.rs", "fn value() {}\n")
        base = self.commit()
        self.write("src/new.rs", "fn added() {}\n")
        index = Path(self.git("rev-parse", "--path-format=absolute", "--git-path", "index"))
        before = index.read_bytes()
        result = self.worktree_check(base)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("check 1: file or module layout changes", result.stdout)
        self.assertEqual(index.read_bytes(), before)
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "")

    def test_scratch_commits_ignore_signing_config_for_worktree_and_merge_folding(self) -> None:
        base = self.rename_fixture()
        _ = self.git("checkout", "-qb", "incoming")
        self.write("incoming.txt", "work merged from the style branch\n")
        _ = self.commit()
        _ = self.git("checkout", "main")
        _ = self.git("merge", "--no-ff", "--no-edit", "incoming")
        _ = self.git("config", "commit.gpgsign", "true")
        _ = self.git("config", "gpg.program", "/nonexistent/style-footprint-signing")
        for path in ("src/lib.rs", "src/first.rs", "src/second.rs"):
            self.write(path, (self.repo / path).read_text().replace("read_value", "load_value"))
        result = self.worktree_check(base)
        self.assertEqual((result.returncode, result.stdout), (0, "mechanical\n"), result.stderr)

    def test_worktree_snapshot_error_cleans_up_its_temporary_index(self) -> None:
        self.write("src/lib.rs", "fn value() {}\n")
        base = self.commit()
        self.write("src/lib.rs", "fn renamed() {}\n")
        original_run = subprocess.run
        indexes: list[Path] = []

        def fail_write_tree(arguments: list[str], *, capture_output: bool, text: bool, check: bool,
                            env: dict[str, str] | None = None, input: str = "",
                            errors: str = "strict") -> subprocess.CompletedProcess[str]:
            environment = env
            if environment is not None and "GIT_INDEX_FILE" in environment:
                path = Path(str(environment["GIT_INDEX_FILE"]))
                indexes.append(path)
                if "write-tree" in arguments:
                    return subprocess.CompletedProcess(arguments, 128, "", "snapshot failed")
            return original_run(arguments, capture_output=capture_output, text=text, check=check,
                                env=env, input=input, errors=errors)

        with mock.patch("style_footprint.subprocess.run", side_effect=fail_write_tree):
            output = io.StringIO()
            with redirect_stdout(output):
                code = style_footprint.main(["check", "--repo", str(self.repo), "--base", base, "--worktree"])
        self.assertEqual((code, output.getvalue()), (1, "review: snapshot failed\n"))
        self.assertTrue(indexes)
        self.assertTrue(all(not path.exists() for path in indexes))

    def test_head_and_worktree_are_required_mutually_exclusive_options(self) -> None:
        self.write("src/lib.rs", "fn value() {}\n")
        base = self.commit()
        for options in ([], ["--head", "HEAD", "--worktree"]):
            with self.subTest(options=options):
                result = subprocess.run([sys.executable, str(SCRIPT), "check", "--repo", str(self.repo),
                                         "--base", base, *options], capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)

    def test_complete_private_rename_in_three_files_is_mechanical(self) -> None:
        base = self.rename_fixture()
        head = self.rename_value()
        self.assert_mechanical(base, head)

    def test_whitespace_only_function_formatting_is_mechanical(self) -> None:
        self.write("src/lib.rs", "fn value(input: usize) -> usize { input }\n")
        base = self.commit()
        self.write("src/lib.rs", "fn value(input: usize) -> usize {\n    input\n}\n")
        _ = self.commit()
        self.assert_mechanical(base)

    def test_complete_rename_used_by_stringify_requires_review(self) -> None:
        before = (
            "fn read_value(input: usize) -> usize { input }\n"
            + "fn name() -> &'static str { stringify!(read_value) }\n"
        )
        self.write("src/lib.rs", before)
        base = self.commit()
        self.write("src/lib.rs", before.replace("read_value", "load_value"))
        _ = self.commit()
        self.assertIn("check 6", self.assert_review(base))

    def test_spacing_change_inside_a_macro_invocation_requires_review(self) -> None:
        self.write("src/lib.rs", "fn f() -> &'static str { stringify!(a+b) }\n")
        base = self.commit()
        self.write("src/lib.rs", "fn f() -> &'static str { stringify!(a + b) }\n")
        _ = self.commit()
        self.assertEqual(self.assert_review(base), "review: check 6: macro invocation requires review\n")

    def test_spacing_change_on_a_multiline_macro_interior_requires_review(self) -> None:
        before = "fn f() -> &'static str {\n    stringify!(\n        a+b\n    )\n}\n"
        self.write("src/lib.rs", before)
        base = self.commit()
        self.write("src/lib.rs", before.replace("a+b", "a + b"))
        _ = self.commit()
        self.assertEqual(self.assert_review(base), "review: check 6: macro invocation requires review\n")

    def test_private_rename_on_a_plain_negation_line_is_mechanical(self) -> None:
        for expression in ("!flag", "return !(flag);"):
            with self.subTest(expression=expression):
                before = (
                    f"fn read_flag(flag: bool) -> bool {{ {expression} }}\n"
                    + "fn inspect(flag: bool) -> bool { read_flag(flag) }\n"
                )
                self.write("src/lib.rs", before)
                base = self.commit()
                self.write("src/lib.rs", before.replace("read_flag", "load_flag"))
                _ = self.commit()
                result = self.check(base)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "mechanical\n", ""))

    def test_multiline_stringify_does_not_prove_a_definition_rename_safe(self) -> None:
        before = (
            "fn read_value(input: usize) -> usize { input }\n"
            + "fn name() -> &'static str {\n    stringify!(\n        read_value\n    )\n}\n"
        )
        self.write("src/lib.rs", before)
        base = self.commit()
        self.write("src/lib.rs", before.replace("read_value", "load_value"))
        _ = self.commit()
        self.assertIn("check 6", self.assert_review(base))

    def test_free_function_rename_cannot_justify_an_unrelated_receiver_method_swap(self) -> None:
        before = (
            "fn front(input: usize) -> usize { input }\n"
            + "fn inspect(cache: &std::collections::VecDeque<usize>) {\n    cache.front();\n}\n"
        )
        self.write("src/lib.rs", before)
        base = self.commit()
        self.write("src/lib.rs", before.replace("front", "back"))
        _ = self.commit()
        self.assertIn("check 6", self.assert_review(base))

    def test_added_deleted_and_moved_files_require_review(self) -> None:
        self.write("src/lib.rs", "fn value(input: usize) -> usize { input }\n")
        base = self.commit()
        for operation in ("add", "delete", "move"):
            with self.subTest(operation=operation):
                _ = self.git("reset", "--hard", base)
                if operation == "add":
                    self.write("src/extra.rs", "fn extra(input: usize) -> usize { input }\n")
                elif operation == "delete":
                    (self.repo / "src/lib.rs").unlink()
                else:
                    _ = self.git("mv", "src/lib.rs", "src/moved.rs")
                head = self.commit()
                self.assertIn("check 1", self.assert_review(base, head))

    def test_module_declaration_change_requires_review(self) -> None:
        self.write("src/lib.rs", "mod first;\n")
        self.write("src/first.rs", "fn value(input: usize) -> usize { input }\n")
        base = self.commit()
        self.write("src/lib.rs", "pub(crate) mod first;\n")
        _ = self.commit()
        self.assertIn("check 1", self.assert_review(base))

    def test_externally_reachable_item_rename_and_signature_change_require_review(self) -> None:
        original = "pub fn read_value(input: usize) -> usize { input }\n"
        self.write("src/lib.rs", original)
        base = self.commit()
        for changed in (original.replace("read_value", "load_value"), original.replace("usize", "u64")):
            with self.subTest(changed=changed):
                _ = self.git("reset", "--hard", base)
                self.write("src/lib.rs", changed)
                _ = self.commit()
                self.assertIn("check 2", self.assert_review(base))

    def test_pub_crate_and_public_item_inside_private_module_can_be_renamed(self) -> None:
        for declaration in ("pub(crate) mod internal;", "mod internal;"):
            with self.subTest(declaration=declaration):
                self.write("src/lib.rs", declaration + "\n")
                self.write("src/internal.rs", "pub fn read_value(input: usize) -> usize { input }\n")
                base = self.commit()
                self.write("src/internal.rs", "pub fn load_value(input: usize) -> usize { input }\n")
                _ = self.commit()
                self.assert_mechanical(base)

    def test_public_item_inside_public_module_requires_review(self) -> None:
        self.write("src/lib.rs", "pub mod internal;\n")
        self.write("src/internal.rs", "pub fn read_value(input: usize) -> usize { input }\n")
        base = self.commit()
        self.write("src/internal.rs", "pub fn load_value(input: usize) -> usize { input }\n")
        _ = self.commit()
        self.assertIn("check 2", self.assert_review(base))

    def test_type_shape_and_trait_implementation_changes_require_review(self) -> None:
        changes = (
            ("struct Value { value: usize }\n", "struct Value { value: usize, extra: usize }\n"),
            ("struct Value { value: usize, extra: usize }\n", "struct Value { value: usize }\n"),
            ("enum Choice { First }\n", "enum Choice { First, Second }\n"),
            ("enum Choice { First, Second }\n", "enum Choice { First }\n"),
            ("struct Value<T> { value: T }\n", "struct Value<T, U> { value: T, extra: U }\n"),
            ("struct Value<T, U> { value: T, extra: U }\n", "struct Value<T> { value: T }\n"),
            ("struct Value;\ntrait Marker {}\n", "struct Value;\ntrait Marker {}\nimpl Marker for Value {}\n"),
            ("struct Value;\ntrait Marker {}\nimpl Marker for Value {}\n", "struct Value;\ntrait Marker {}\n"),
            ("fn value(input: usize) -> usize { input }\n", "struct Value;\nfn value(input: usize) -> usize { input }\n"),
            ("fn value(input: usize) -> usize { input }\n", "enum Choice { First }\nfn value(input: usize) -> usize { input }\n"),
            ("fn value(input: usize) -> usize { input }\n", "trait Marker {}\nfn value(input: usize) -> usize { input }\n"),
            ("fn value(input: usize) -> usize { input }\n", "struct ReadError;\nfn value(input: usize) -> usize { input }\n"),
        )
        for before, after in changes:
            with self.subTest(before=before, after=after):
                self.write("src/lib.rs", before)
                base = self.commit()
                self.write("src/lib.rs", after)
                _ = self.commit()
                self.assertIn("check 3", self.assert_review(base))

    def test_rename_in_eleven_files_requires_review(self) -> None:
        modules = tuple(f"part{index}" for index in range(10))
        self.write("src/lib.rs", "".join(f"mod {module};\n" for module in modules)
                   + "fn read_value(input: usize) -> usize { input }\n")
        for module in modules:
            self.write(f"src/{module}.rs", "fn use_value(input: usize) -> usize { super::read_value(input) }\n")
        base = self.commit()
        for path in (self.repo / "src").glob("*.rs"):
            _ = path.write_text(path.read_text().replace("read_value", "load_value"))
        _ = self.commit()
        self.assertIn("check 4", self.assert_review(base))

    def test_rename_spanning_two_crates_requires_review(self) -> None:
        self.write("Cargo.toml", '[workspace]\nmembers = ["crates/first", "crates/second"]\n')
        before = "fn read_value(input: usize) -> usize { input }\nfn use_value(input: usize) -> usize { read_value(input) }\n"
        for crate in ("first", "second"):
            self.write(f"crates/{crate}/Cargo.toml", f'[package]\nname = "{crate}"\nversion = "0.1.0"\n')
            self.write(f"crates/{crate}/src/lib.rs", before)
        base = self.commit()
        for crate in ("first", "second"):
            self.write(f"crates/{crate}/src/lib.rs", before.replace("read_value", "load_value"))
        _ = self.commit()
        self.assertIn("check 4", self.assert_review(base))

    def test_complete_rename_in_ten_files_is_within_the_file_limit(self) -> None:
        modules = tuple(f"part{index}" for index in range(9))
        self.write("src/lib.rs", "".join(f"mod {module};\n" for module in modules)
                   + "fn read_value(input: usize) -> usize { input }\n")
        for module in modules:
            self.write(f"src/{module}.rs", "fn use_value(input: usize) -> usize { super::read_value(input) }\n")
        base = self.commit()
        for path in (self.repo / "src").glob("*.rs"):
            _ = path.write_text(path.read_text().replace("read_value", "load_value"))
        _ = self.commit()
        self.assert_mechanical(base)

    def test_changed_line_limit_is_strictly_under_two_hundred(self) -> None:
        for count in (99, 100):
            with self.subTest(changed_lines=count * 2):
                before = "fn read_value(input: usize) -> usize { input }\nfn use_value(input: usize) {\n"
                before += "    read_value(input);\n" * (count - 1) + "}\n"
                self.write("src/lib.rs", before)
                base = self.commit()
                self.write("src/lib.rs", before.replace("read_value", "load_value"))
                _ = self.commit()
                if count == 99:
                    self.assert_mechanical(base)
                else:
                    self.assertIn("check 5", self.assert_review(base))

    def test_condition_literal_control_flow_and_serialized_name_changes_require_review(self) -> None:
        changes = (
            ("fn value(input: usize) -> usize { if input > 1 { input } else { 1 } }\n",
             "fn value(input: usize) -> usize { if input >= 1 { input } else { 1 } }\n"),
            ("fn value() -> usize { 1 }\n", "fn value() -> usize { 2 }\n"),
            ("fn value(input: bool) { while input { work(); } }\n", "fn value(input: bool) { while !input { work(); } }\n"),
            ("fn value(input: usize) -> usize { match input { n if n > 1 => n, _ => 1 } }\n",
             "fn value(input: usize) -> usize { match input { n if n >= 1 => n, _ => 1 } }\n"),
            ('#[serde(rename = "before")]\nstruct Value;\n', '#[serde(rename = "after")]\nstruct Value;\n'),
            ('#[reflect(rename = "before")]\nstruct Value;\n', '#[reflect(rename = "after")]\nstruct Value;\n'),
            ('fn value() { configure("before"); }\n', 'fn value() { configure("after"); }\n'),
        )
        for before, after in changes:
            with self.subTest(before=before):
                self.write("src/lib.rs", before)
                base = self.commit()
                self.write("src/lib.rs", after)
                _ = self.commit()
                self.assertIn("check 6", self.assert_review(base))

    def test_method_target_swap_without_a_definition_rename_requires_review(self) -> None:
        before = (
            "struct Cache;\nimpl Cache {\n    fn clear(&mut self) {}\n    fn flush(&mut self) {}\n}\n"
            + "fn clean(cache: &mut Cache) { cache.clear(); }\n"
        )
        self.write("src/lib.rs", before)
        base = self.commit()
        self.write("src/lib.rs", before.replace("cache.clear()", "cache.flush()"))
        _ = self.commit()
        self.assertIn("check 6: call target, method or other change not proven equivalent", self.assert_review(base))

    def test_local_rename_refuses_a_different_call_in_its_initializer(self) -> None:
        before = ("fn total() -> u32 { 1 }\nfn sum() -> u32 { 2 }\n"
                  + "fn f() -> u32 { let total = total(); total }\n")
        self.write("src/lib.rs", before)
        base = self.commit()
        self.write("src/lib.rs", before.replace("let total = total(); total", "let sum = sum(); sum"))
        _ = self.commit()
        self.assertIn("check 6", self.assert_review(base))

    def test_local_rename_refuses_a_different_call_before_its_declaration(self) -> None:
        before = ("fn total() -> usize { 1 }\nfn sum() -> usize { 2 }\n"
                  + "fn f() -> usize { let a = total(); let total = 2; a + total }\n")
        self.write("src/lib.rs", before)
        base = self.commit()
        self.write("src/lib.rs", before.replace(
            "let a = total(); let total = 2; a + total", "let a = sum(); let sum = 2; a + sum",
        ))
        _ = self.commit()
        self.assertIn("check 6", self.assert_review(base))

    def test_local_rename_cannot_move_a_value_to_a_different_struct_field(self) -> None:
        before = ("#[derive(Default)]\nstruct Foo { total: u32, sum: u32 }\n"
                  + "fn f() -> Foo { let total = 3; Foo { total: total, ..Default::default() } }\n")
        self.write("src/lib.rs", before)
        base = self.commit()
        self.write("src/lib.rs", before.replace(
            "let total = 3; Foo { total: total", "let sum = 3; Foo { sum: sum",
        ))
        _ = self.commit()
        self.assertIn("check 6", self.assert_review(base))

    def test_import_shortening_cannot_bring_a_trait_into_method_resolution(self) -> None:
        self.write("src/lib.rs", "mod traits;\nmod reader;\n")
        self.write("src/traits.rs", "pub(crate) trait Choice { fn answer(&self) -> u32; }\n"
                   + "impl Choice for crate::reader::Widget { fn answer(&self) -> u32 { 2 } }\n")
        before = ("pub(crate) struct Widget;\nimpl Widget { fn answer(&self) -> u32 { 1 } }\n"
                  + "fn read(w: &Widget, choice: &dyn crate::traits::Choice) -> u32 { w.answer() }\n")
        self.write("src/reader.rs", before)
        base = self.commit()
        self.write("src/reader.rs", "use crate::traits::Choice;\n" + before.replace(
            "&dyn crate::traits::Choice", "&dyn Choice",
        ))
        _ = self.commit()
        self.assertIn("check 6", self.assert_review(base))

    def test_enum_variant_rename_cannot_change_a_same_named_structs_constant(self) -> None:
        before = ("enum Mode { Old, Other }\n"
                  + "mod other { pub struct Mode; impl Mode { pub const Old: u32 = 1; pub const New: u32 = 2; } }\n"
                  + "fn selected() -> Mode { Mode::Old }\n"
                  + "fn unrelated() -> u32 { other::Mode::Old }\n")
        self.write("src/lib.rs", before)
        base = self.commit()
        self.write("src/lib.rs", before.replace("enum Mode { Old", "enum Mode { New")
                   .replace("Mode::Old", "Mode::New"))
        _ = self.commit()
        self.assertIn("check 6", self.assert_review(base))

    def test_constant_import_shortening_allows_an_unchanged_public_reexport(self) -> None:
        self.write("src/lib.rs", "mod columns;\nmod reader;\n")
        self.write("src/columns.rs", "pub(crate) const COL_DISK: usize = 1;\n")
        before = ("pub use std::fmt::Debug;\nuse crate::columns;\n"
                  + "fn read() -> usize { columns::COL_DISK }\n")
        self.write("src/reader.rs", before)
        base = self.commit()
        self.write("src/reader.rs", "use crate::columns::COL_DISK;\n"
                   + before.replace("{ columns::COL_DISK }", "{ COL_DISK }"))
        _ = self.commit()
        self.assert_mechanical(base)

    def test_constant_import_shortening_can_leave_another_qualified_site(self) -> None:
        self.write("src/lib.rs", "mod columns;\nmod reader;\n")
        self.write("src/columns.rs", "pub(crate) const COL_DISK: usize = 1;\n")
        before = ("use super::columns;\nfn read() -> usize { columns::COL_DISK }\n"
                  + "fn other() -> usize { columns::COL_DISK }\n")
        self.write("src/reader.rs", before)
        base = self.commit()
        self.write("src/reader.rs", "use super::columns::COL_DISK;\n"
                   + before.replace("{ columns::COL_DISK }", "{ COL_DISK }", 1))
        _ = self.commit()
        self.assert_mechanical(base)

    def test_import_scope_allows_an_explicit_import_beside_a_module_glob(self) -> None:
        self.write("src/constants.rs", 'pub(crate) const KEYMAP_FILE: &str = "keys";\n')
        before = ("mod constants;\n"
                  + "#[cfg(test)]\nmod tests {\n    use super::*;\n"
                  + "    fn first() -> &'static str { crate::constants::KEYMAP_FILE }\n"
                  + "    fn second() -> &'static str { crate::constants::KEYMAP_FILE }\n}\n")
        self.write("src/lib.rs", before)
        base = self.commit()
        after = before.replace("    use super::*;", "    use super::*;\n    use crate::constants::KEYMAP_FILE;")
        self.write("src/lib.rs", after.replace("{ crate::constants::KEYMAP_FILE }", "{ KEYMAP_FILE }"))
        _ = self.commit()
        self.assert_mechanical(base)

    def test_import_scope_allows_a_root_constant_with_a_child_module_glob(self) -> None:
        self.write("src/constants.rs", 'pub(crate) const LINTS_PAUSED_MARKER: &str = "paused";\n')
        before = ("mod constants;\n"
                  + "fn marker() -> &'static str { crate::constants::LINTS_PAUSED_MARKER }\n"
                  + "#[cfg(test)]\nmod tests { use super::*; fn check() {} }\n")
        self.write("src/lib.rs", before)
        base = self.commit()
        self.write("src/lib.rs", "use crate::constants::LINTS_PAUSED_MARKER;\n" + before.replace(
            "{ crate::constants::LINTS_PAUSED_MARKER }", "{ LINTS_PAUSED_MARKER }",
        ))
        _ = self.commit()
        self.assert_mechanical(base)

    def test_import_scope_allows_a_std_constant_with_a_child_module_glob(self) -> None:
        before = ("fn epoch() -> std::time::SystemTime { std::time::UNIX_EPOCH }\n"
                  + "#[cfg(test)]\nmod tests { use super::*; fn check() {} }\n")
        self.write("src/lib.rs", before)
        base = self.commit()
        self.write("src/lib.rs", "use std::time::UNIX_EPOCH;\n" + before.replace(
            "{ std::time::UNIX_EPOCH }", "{ UNIX_EPOCH }",
        ))
        _ = self.commit()
        self.assert_mechanical(base)

    def test_import_scope_refuses_a_glob_in_the_site_function(self) -> None:
        self.write("src/constants.rs", 'pub(crate) const KEYMAP_FILE: &str = "keys";\n')
        before = ("mod constants;\n"
                  + "fn marker() -> &'static str { use self::constants::*; crate::constants::KEYMAP_FILE }\n")
        self.write("src/lib.rs", before)
        base = self.commit()
        self.write("src/lib.rs", "use crate::constants::KEYMAP_FILE;\n" + before.replace(
            "; crate::constants::KEYMAP_FILE }", "; KEYMAP_FILE }",
        ))
        _ = self.commit()
        _ = self.assert_review(base)

    def test_import_scope_refuses_sites_in_a_different_module(self) -> None:
        self.write("src/lib.rs", "mod constants;\nmod columns;\nmod reader;\n")
        self.write("src/constants.rs", "pub(crate) const COL_DISK: usize = 1;\n")
        self.write("src/columns.rs", "pub(crate) const COL_DISK: usize = 1;\n")
        self.write("src/reader/columns.rs", "pub(crate) const COL_DISK: usize = 2;\n")
        for qualified, glob in (
            ("crate::constants::COL_DISK", ""),
            ("super::columns::COL_DISK", "use super::*; "),
        ):
            with self.subTest(qualified=qualified):
                before = ("mod columns;\n"
                          + "mod tests { " + glob + "fn read() -> usize { " + qualified + " } }\n")
                self.write("src/reader.rs", before)
                base = self.commit()
                self.write("src/reader.rs", "use " + qualified + ";\n" + before.replace(
                    "{ " + qualified + " }", "{ COL_DISK }",
                ))
                _ = self.commit()
                _ = self.assert_review(base)

    def test_import_scope_refuses_an_import_inside_a_function(self) -> None:
        before = "fn epoch() -> std::time::SystemTime { std::time::UNIX_EPOCH }\n"
        self.write("src/lib.rs", before)
        base = self.commit()
        self.write("src/lib.rs", before.replace(
            "{ std::time::UNIX_EPOCH }", "{ use std::time::UNIX_EPOCH; UNIX_EPOCH }",
        ))
        _ = self.commit()
        _ = self.assert_review(base)

    def test_import_scope_keeps_plain_root_shortening_mechanical(self) -> None:
        self.write("src/constants.rs", 'pub(crate) const KEYMAP_FILE: &str = "keys";\n')
        before = ("mod constants;\n"
                  + "fn marker() -> &'static str { crate::constants::KEYMAP_FILE }\n")
        self.write("src/lib.rs", before)
        base = self.commit()
        self.write("src/lib.rs", "use crate::constants::KEYMAP_FILE;\n" + before.replace(
            "{ crate::constants::KEYMAP_FILE }", "{ KEYMAP_FILE }",
        ))
        _ = self.commit()
        self.assert_mechanical(base)

    def test_import_cfg_accepts_the_same_guard_on_the_import_and_function(self) -> None:
        before = ('#[cfg(target_os = "macos")]\n'
                  + "fn query_macos_monotonic_process_start() -> u32 { libc::RUSAGE_INFO_V0 }\n")
        self.write("src/lib.rs", before)
        base = self.commit()
        self.write("src/lib.rs", '#[cfg(target_os = "macos")]\nuse libc::RUSAGE_INFO_V0;\n'
                   + before.replace("{ libc::RUSAGE_INFO_V0 }", "{ RUSAGE_INFO_V0 }"))
        _ = self.commit()
        self.assert_mechanical(base)

    def test_import_cfg_accepts_matching_guards_with_a_known_std_root(self) -> None:
        before = '#[cfg(unix)]\nfn epoch() -> std::time::SystemTime { std::time::UNIX_EPOCH }\n'
        self.write("src/lib.rs", before)
        base = self.commit()
        self.write("src/lib.rs", "#[cfg(unix)]\nuse std::time::UNIX_EPOCH;\n" + before.replace(
            "{ std::time::UNIX_EPOCH }", "{ UNIX_EPOCH }",
        ))
        _ = self.commit()
        self.assert_mechanical(base)

    def test_import_cfg_refuses_missing_or_different_site_guards(self) -> None:
        for guard in ("", "#[cfg(unix)]\n"):
            with self.subTest(guard=guard):
                before = guard + "fn epoch() -> std::time::SystemTime { std::time::UNIX_EPOCH }\n"
                self.write("src/lib.rs", before)
                base = self.commit()
                self.write("src/lib.rs", '#[cfg(target_os = "macos")]\nuse std::time::UNIX_EPOCH;\n'
                           + before.replace("{ std::time::UNIX_EPOCH }", "{ UNIX_EPOCH }"))
                _ = self.commit()
                _ = self.assert_review(base)

    def test_import_cfg_refuses_other_or_multiple_import_attributes(self) -> None:
        for attributes in (
            "#[allow(unused_imports)]\n",
            "#[cfg_attr(unix, allow(unused_imports))]\n",
            "#[cfg(unix)]\n#[allow(unused_imports)]\n",
            "#[cfg(unix)]\n#[cfg(unix)]\n",
        ):
            with self.subTest(attributes=attributes):
                before = '#[cfg(unix)]\nfn epoch() -> std::time::SystemTime { std::time::UNIX_EPOCH }\n'
                self.write("src/lib.rs", before)
                base = self.commit()
                self.write("src/lib.rs", attributes + "use std::time::UNIX_EPOCH;\n" + before.replace(
                    "{ std::time::UNIX_EPOCH }", "{ UNIX_EPOCH }",
                ))
                _ = self.commit()
                _ = self.assert_review(base)

    def test_import_external_root_accepts_a_word_used_only_as_a_path_root(self) -> None:
        before = "fn query() -> u32 { libc::RUSAGE_INFO_V0 }\n"
        self.write("src/lib.rs", before)
        base = self.commit()
        self.write("src/lib.rs", "use libc::RUSAGE_INFO_V0;\n" + before.replace(
            "{ libc::RUSAGE_INFO_V0 }", "{ RUSAGE_INFO_V0 }",
        ))
        _ = self.commit()
        self.assert_mechanical(base)

    def test_import_external_root_refuses_words_with_other_roles(self) -> None:
        for other in (
            "mod libc {}\n",
            "use a::libc;\n",
            "extern crate libc;\n",
            "fn other() { let libc = 1; consume(libc); }\n",
            "use a::{libc, Other};\n",
            "use a::other as libc;\n",
            "fn other() { consume(a::libc::OTHER); }\n",
            "fn other() { libc!(); }\n",
        ):
            with self.subTest(other=other):
                before = other + "fn query() -> u32 { libc::RUSAGE_INFO_V0 }\n"
                self.write("src/lib.rs", before)
                base = self.commit()
                self.write("src/lib.rs", "use libc::RUSAGE_INFO_V0;\n" + before.replace(
                    "{ libc::RUSAGE_INFO_V0 }", "{ RUSAGE_INFO_V0 }",
                ))
                _ = self.commit()
                _ = self.assert_review(base)

    def test_plain_local_rename_with_uses_after_its_statement_is_mechanical(self) -> None:
        before = "fn read() -> u32 { let total = 3; total + total }\n"
        self.write("src/lib.rs", before)
        base = self.commit()
        self.write("src/lib.rs", before.replace("total", "sum"))
        _ = self.commit()
        self.assert_mechanical(base)

    def test_plain_unqualified_enum_variant_rename_is_mechanical(self) -> None:
        before = "enum Mode { Old, Other }\nfn selected() -> Mode { Mode::Old }\n"
        self.write("src/lib.rs", before)
        base = self.commit()
        self.write("src/lib.rs", before.replace("Old", "New"))
        _ = self.commit()
        self.assert_mechanical(base)

    def test_local_rename_refuses_uses_outside_the_binding_block(self) -> None:
        before = ("fn total() -> u32 { 1 }\nfn sum() -> u32 { 2 }\n"
                  + "fn read() -> u32 { { let total = 3; consume(total); } total() }\n")
        self.write("src/lib.rs", before)
        base = self.commit()
        self.write("src/lib.rs", before.replace(
            "{ let total = 3; consume(total); } total()", "{ let sum = 3; consume(sum); } sum()",
        ))
        _ = self.commit()
        _ = self.assert_review(base)

    def test_local_rename_refuses_let_conditions_nested_functions_and_macro_definitions(self) -> None:
        for body in (
            "if let Some(total) = input { consume(total); }",
            "let total = input; fn nested() {} consume(total);",
            "let total = input; macro_rules! nested { () => {} } consume(total);",
        ):
            with self.subTest(body=body):
                before = "fn read(input: Option<u32>) { " + body + " }\n"
                self.write("src/lib.rs", before)
                base = self.commit()
                self.write("src/lib.rs", before.replace("total", "sum"))
                _ = self.commit()
                _ = self.assert_review(base)

    def test_local_rename_refuses_implicit_format_captures(self) -> None:
        for literal in ('"{total}"', '"{total:04}"'):
            with self.subTest(literal=literal):
                before = "fn read() { let total = 3; consume(total); consume(" + literal + "); }\n"
                self.write("src/lib.rs", before)
                base = self.commit()
                self.write("src/lib.rs", before.replace("let total", "let sum").replace(
                    "consume(total)", "consume(sum)",
                ))
                _ = self.commit()
                _ = self.assert_review(base)

    def test_local_rename_accepts_shadowing_lets_and_nested_initializer_semicolons(self) -> None:
        for body in (
            "let total = 3; let total = 4; total",
            "let total = { let input = 3; input }; total",
        ):
            with self.subTest(body=body):
                before = "fn read() -> u32 { " + body + " }\n"
                self.write("src/lib.rs", before)
                base = self.commit()
                self.write("src/lib.rs", before.replace("total", "sum"))
                _ = self.commit()
                self.assert_mechanical(base)

    def test_local_rename_cannot_certify_member_or_qualified_names(self) -> None:
        for expression in ("other.total", "other::total", "total::VALUE"):
            with self.subTest(expression=expression):
                before = "fn read() { let total = 3; consume(total); consume(" + expression + "); }\n"
                self.write("src/lib.rs", before)
                base = self.commit()
                self.write("src/lib.rs", before.replace("total", "sum"))
                _ = self.commit()
                _ = self.assert_review(base)

    def test_enum_variant_rename_requires_a_plain_enum_import_in_other_files(self) -> None:
        for imported in ("", "use external::Mode;\n", "use crate::Mode as Alias;\n",
                         "#[cfg(test)]\nuse crate::Mode;\n", "use crate::Mode;\nuse crate::*;\n"):
            with self.subTest(imported=imported):
                self.write("src/lib.rs", "mod reader;\nenum Mode { Old, Other }\n")
                self.write("src/reader.rs", imported + "fn read() -> Mode { Mode::Old }\n")
                base = self.commit()
                self.write("src/lib.rs", "mod reader;\nenum Mode { New, Other }\n")
                self.write("src/reader.rs", imported + "fn read() -> Mode { Mode::New }\n")
                _ = self.commit()
                _ = self.assert_review(base)

    def test_enum_variant_rename_accepts_a_plain_enum_import_in_another_file(self) -> None:
        self.write("src/lib.rs", "mod reader;\nenum Mode { Old, Other }\n")
        self.write("src/reader.rs", "use crate::Mode;\nfn read() -> Mode { Mode::Old }\n")
        base = self.commit()
        self.write("src/lib.rs", "mod reader;\nenum Mode { New, Other }\n")
        self.write("src/reader.rs", "use crate::Mode;\nfn read() -> Mode { Mode::New }\n")
        _ = self.commit()
        self.assert_mechanical(base)

    def test_constant_import_shortening_allows_unrelated_unchanged_complex_uses(self) -> None:
        for unchanged in ("use std::fmt::Debug as DebugTrait;",
                          "#[cfg(test)]\nuse std::fmt::Debug;", "pub(super) use std::fmt::Debug;"):
            with self.subTest(unchanged=unchanged):
                before = unchanged + "\nfn read() -> usize { crate::columns::COL_DISK }\n"
                self.write("src/lib.rs", "mod reader;\nmod columns;\n")
                self.write("src/columns.rs", "pub(crate) const COL_DISK: usize = 1;\n")
                self.write("src/reader.rs", before)
                base = self.commit()
                self.write("src/reader.rs", "use crate::columns::COL_DISK;\n"
                           + before.replace("{ crate::columns::COL_DISK }", "{ COL_DISK }"))
                _ = self.commit()
                self.assert_mechanical(base)

    def test_constant_import_shortening_refuses_to_resolve_through_complex_uses(self) -> None:
        for imported in ("pub use crate::columns;", "#[cfg(test)]\nuse crate::columns;",
                         "use crate::columns as columns;"):
            with self.subTest(imported=imported):
                before = imported + "\nfn read() -> usize { columns::COL_DISK }\n"
                self.write("src/lib.rs", "mod reader;\nmod columns;\n")
                self.write("src/columns.rs", "pub(crate) const COL_DISK: usize = 1;\n")
                self.write("src/reader.rs", before)
                base = self.commit()
                self.write("src/reader.rs", "use crate::columns::COL_DISK;\n"
                           + before.replace("{ columns::COL_DISK }", "{ COL_DISK }"))
                _ = self.commit()
                _ = self.assert_review(base)

    def test_lint_name_removal_from_allow_and_expect_lists_is_mechanical(self) -> None:
        for level in ("allow", "expect"):
            for marker in ("#", "#!"):
                for names in ("clippy::expect_used, clippy::panic",
                              "clippy::expect_used, clippy::panic, dead_code"):
                    with self.subTest(level=level, marker=marker, names=names):
                        attribute = f"{marker}[{level}({names})]\n"
                        body = "mod tests { fn example() {} }\n" if marker == "#" else "fn example() {}\n"
                        self.write("src/lib.rs", attribute + body)
                        base = self.commit()
                        self.write("src/lib.rs", f"{marker}[{level}(clippy::panic)]\n" + body)
                        _ = self.commit()
                        self.assert_mechanical(base)

    def test_whole_lint_attribute_removal_is_mechanical(self) -> None:
        for level in ("allow", "expect"):
            for marker in ("#", "#!"):
                for reason in ("", ', reason = "checked elsewhere"'):
                    with self.subTest(level=level, marker=marker, reason=reason):
                        body = "mod tests { fn example() {} }\n" if marker == "#" else "fn example() {}\n"
                        self.write("src/lib.rs", f"{marker}[{level}(clippy::panic{reason})]\n" + body)
                        base = self.commit()
                        self.write("src/lib.rs", body)
                        _ = self.commit()
                        self.assert_mechanical(base)

    def test_lint_name_removal_preserves_reason_and_multiline_attributes(self) -> None:
        for level in ("allow", "expect"):
            with self.subTest(level=level):
                before = (f"#[{level}(\n    clippy::expect_used,\n    clippy::panic,\n"
                          + '    reason = "test helper",\n)]\nmod tests { fn example() {} }\n')
                self.write("src/lib.rs", before)
                base = self.commit()
                self.write("src/lib.rs", before.replace("    clippy::expect_used,\n", ""))
                _ = self.commit()
                self.assert_mechanical(base)

    def test_adding_or_replacing_a_lint_name_requires_review(self) -> None:
        for level in ("allow", "expect"):
            for names in ("clippy::panic, clippy::expect_used", "clippy::expect_used"):
                with self.subTest(level=level, names=names):
                    body = "\nmod tests { fn example() {} }\n"
                    self.write("src/lib.rs", f"#[{level}(clippy::panic)]" + body)
                    base = self.commit()
                    self.write("src/lib.rs", f"#[{level}({names})]" + body)
                    _ = self.commit()
                    _ = self.assert_review(base)

    def test_lint_level_changes_require_review(self) -> None:
        for before_level, after_level in (
            ("allow", "expect"), ("expect", "allow"),
            ("allow", "warn"), ("warn", "allow"),
            ("allow", "deny"), ("deny", "allow"),
            ("expect", "forbid"), ("forbid", "expect"),
        ):
            with self.subTest(before=before_level, after=after_level):
                body = "\nmod tests { fn example() {} }\n"
                self.write("src/lib.rs", f"#[{before_level}(clippy::panic)]" + body)
                base = self.commit()
                self.write("src/lib.rs", f"#[{after_level}(clippy::panic)]" + body)
                _ = self.commit()
                _ = self.assert_review(base)

    def test_removing_other_attributes_requires_review(self) -> None:
        for attribute in (
            "#[cfg(test)]", "#[cfg_attr(test, allow(clippy::panic))]",
            "#[derive(Clone)]", '#[serde(rename = "example")]',
            "#[repr(C)]", "#[test]", '#[doc = "example"]', "#[custom(clippy::panic)]",
            "#[warn(clippy::panic)]", "#[deny(clippy::panic)]", "#[forbid(clippy::panic)]",
        ):
            with self.subTest(attribute=attribute):
                body = "fn example() {}\n" if attribute == "#[test]" else "struct Example;\n"
                self.write("src/lib.rs", attribute + "\n" + body)
                base = self.commit()
                self.write("src/lib.rs", body)
                _ = self.commit()
                _ = self.assert_review(base)

    def test_lint_removal_cannot_hide_an_unproven_change_in_the_same_hunk(self) -> None:
        for before_body, after_body in (
            ("fn example() -> usize { 1 }\n", "fn example() -> usize { 2 }\n"),
            ("fn example(input: usize) { if input > 1 {} }\n",
             "fn example(input: usize) { if input >= 1 {} }\n"),
            ("fn example(cache: &mut Cache) { cache.clear(); }\n",
             "fn example(cache: &mut Cache) { cache.flush(); }\n"),
            ("fn example() { assert_eq!(1, 1); }\n", "fn example() { assert_eq!(1, 2); }\n"),
        ):
            with self.subTest(before=before_body):
                self.write("src/lib.rs", "#[allow(clippy::panic)]\n" + before_body)
                base = self.commit()
                self.write("src/lib.rs", after_body)
                _ = self.commit()
                self.assertIn("check 6", self.assert_review(base))

    def test_lint_removal_does_not_allow_changing_a_reason(self) -> None:
        before = ('#[allow(clippy::expect_used, clippy::panic, reason = "test helper")]\n'
                  + "mod tests { fn example() {} }\n")
        self.write("src/lib.rs", before)
        base = self.commit()
        self.write("src/lib.rs", before.replace("clippy::expect_used, ", "")
                   .replace('"test helper"', '"different reason"'))
        _ = self.commit()
        _ = self.assert_review(base)

    def test_whole_lint_attribute_removal_refuses_non_lint_arguments(self) -> None:
        for argument in ('note = "test helper"', 'reason = 7', 'clippy::panic("test helper")'):
            with self.subTest(argument=argument):
                body = "mod tests { fn example() {} }\n"
                self.write("src/lib.rs", f"#[allow(clippy::panic, {argument})]\n" + body)
                base = self.commit()
                self.write("src/lib.rs", body)
                _ = self.commit()
                _ = self.assert_review(base)

    def test_lint_removal_can_share_a_hunk_with_a_proven_local_rename(self) -> None:
        before = ("#[allow(clippy::panic)]\n"
                  + "fn example(input: usize) -> usize { let total = input; total }\n")
        self.write("src/lib.rs", before)
        base = self.commit()
        self.write("src/lib.rs", before.replace("#[allow(clippy::panic)]\n", "").replace("total", "sum"))
        _ = self.commit()
        self.assert_mechanical(base)

    def test_import_rewrites_resolve_existing_paths_and_brace_groups(self) -> None:
        for use, expression, added in (
            ("use crate::columns;", "columns::COL_DISK", "use crate::columns::COL_DISK;"),
            ("use crate::columns;", "columns::COL_DISK + columns::COL_CPU",
             "use crate::columns::{COL_DISK, COL_CPU};"),
            ("", "std::cmp::Ordering::Equal", "use std::cmp::Ordering::Equal;"),
            ("", "super::columns::COL_DISK", "use super::columns::COL_DISK;"),
            ("", "crate::columns::COL_DISK", "use crate::columns::COL_DISK;"),
            ("use crate::columns;", "self::columns::COL_DISK", "use self::columns::COL_DISK;"),
            ("", "core::cmp::Ordering::Equal", "use core::cmp::Ordering::Equal;"),
        ):
            with self.subTest(expression=expression):
                self.write("src/lib.rs", "mod columns;\nmod reader;\n")
                self.write("src/columns.rs", "pub(crate) const COL_DISK: usize = 1;\n"
                           + "pub(crate) const COL_CPU: usize = 2;\n")
                before = use + "\nfn read() { consume(" + expression + "); }\n"
                self.write("src/reader.rs", before)
                base = self.commit()
                shortened = "COL_DISK + COL_CPU" if " + " in expression else expression.split("::")[-1]
                after = before.replace(expression, shortened)
                self.write("src/reader.rs", added + "\n" + after)
                _ = self.commit()
                if expression.endswith("::Equal"):
                    _ = self.assert_review(base)
                else:
                    self.assert_mechanical(base)

    def test_import_rewrite_can_remove_an_unused_qualifier_import(self) -> None:
        self.write("src/lib.rs", "mod columns;\nmod reader;\n")
        self.write("src/columns.rs", "pub(crate) const COL_DISK: usize = 1;\n")
        before = "use crate::columns;\nfn read() { consume(columns::COL_DISK); }\n"
        self.write("src/reader.rs", before)
        base = self.commit()
        self.write("src/reader.rs", "use crate::columns::COL_DISK;\nfn read() { consume(COL_DISK); }\n")
        _ = self.commit()
        self.assert_mechanical(base)

    def test_import_rewrite_refuses_unproven_or_conflicting_imports(self) -> None:
        cases = (
            ("", "mystery::COL_DISK", "use crate::columns::COL_DISK;", ""),
            ("use crate::columns;", "columns::COL_DISK", "use crate::other::COL_DISK;", ""),
            ("use crate::columns;", "columns::COL_DISK", "use crate::columns::COL_DISK;",
             "fn already_bare() { consume(COL_DISK); }\n"),
            ("use crate::columns;", "columns::COL_DISK", "use crate::columns::COL_DISK;",
             "const COL_DISK: usize = 7;\n"),
            ("use crate::columns;", "columns::COL_DISK",
             '#[cfg(feature = "disk")]\nuse crate::columns::COL_DISK;', ""),
        )
        for use, expression, added, other in cases:
            with self.subTest(use=use, added=added, other=other):
                self.write("src/lib.rs", "mod columns;\nmod other;\nmod reader;\n")
                self.write("src/columns.rs", "pub(crate) const COL_DISK: usize = 1;\n")
                self.write("src/other.rs", "pub(crate) const COL_DISK: usize = 2;\n")
                before = use + "\n" + other + "fn read() { consume(" + expression + "); }\n"
                self.write("src/reader.rs", before)
                base = self.commit()
                self.write("src/reader.rs", added + "\n" + before.replace(expression, "COL_DISK"))
                _ = self.commit()
                self.assertIn("check 6", self.assert_review(base))

    def test_added_explicit_import_beats_a_glob_in_the_same_module(self) -> None:
        self.write("src/lib.rs", "mod columns;\nmod other;\nmod reader;\n")
        self.write("src/columns.rs", "pub(crate) const COL_DISK: usize = 1;\n")
        self.write("src/other.rs", "pub(crate) const COL_DISK: usize = 2;\n")
        before = ("use crate::columns;\nuse crate::other::*;\n"
                  + "fn read() { consume(columns::COL_DISK); }\n")
        self.write("src/reader.rs", before)
        base = self.commit()
        self.write("src/reader.rs", "use crate::columns::COL_DISK;\n"
                   + before.replace("columns::COL_DISK", "COL_DISK"))
        _ = self.commit()
        self.assert_mechanical(base)

    def test_import_rewrite_refuses_removing_a_still_used_import(self) -> None:
        before = ("use crate::columns;\n"
                  "fn read() { consume(columns::COL_DISK); consume(columns::COL_CPU); }\n")
        self.write("src/lib.rs", "mod columns;\nmod reader;\n")
        self.write("src/columns.rs", "pub(crate) const COL_DISK: usize = 1;\n"
                   + "pub(crate) const COL_CPU: usize = 2;\n")
        self.write("src/reader.rs", before)
        base = self.commit()
        self.write("src/reader.rs", "use crate::columns::COL_DISK;\n"
                   + "fn read() { consume(COL_DISK); consume(columns::COL_CPU); }\n")
        _ = self.commit()
        self.assertIn("check 6", self.assert_review(base))

    def test_import_rewrite_does_not_allow_an_unrelated_added_use(self) -> None:
        before = "use crate::columns;\nfn read() { consume(columns::COL_DISK); }\n"
        self.write("src/lib.rs", "mod columns;\nmod reader;\n")
        self.write("src/columns.rs", "pub(crate) const COL_DISK: usize = 1;\n")
        self.write("src/reader.rs", before)
        base = self.commit()
        self.write("src/reader.rs", "use crate::columns::COL_DISK;\nuse std::path::Path;\n"
                   + before.replace("columns::COL_DISK", "COL_DISK"))
        _ = self.commit()
        self.assertIn("check 6", self.assert_review(base))

    def test_enum_variant_rename_supports_enum_self_and_import_sites(self) -> None:
        before = (
            "#[derive(Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Debug)]\n"
            "pub(crate) enum Mode { Old, Other }\n"
            "impl Mode { fn selected() -> Self { Self::Old } }\n"
            "fn select() -> Mode { Mode::Old }\n"
        )
        self.write("src/lib.rs", "mod choices;\nmod direct;\nmod grouped;\n")
        self.write("src/choices.rs", before)
        self.write("src/direct.rs", "use crate::choices::Mode;\nuse crate::choices::Mode::Old;\nfn read() { consume(Old); }\n")
        self.write("src/grouped.rs", "use crate::choices::Mode;\nuse crate::choices::Mode::{Old};\nfn read() { consume(Old); }\n")
        base = self.commit()
        for file in ("src/choices.rs", "src/direct.rs", "src/grouped.rs"):
            self.write(file, (self.repo / file).read_text().replace("Old", "New"))
        _ = self.commit()
        self.assert_mechanical(base)

    def test_enum_variant_rename_with_default_derive_is_mechanical(self) -> None:
        before = ("#[derive(Default)]\nenum Mode { Old, #[default] Other }\n"
                  "fn select() -> Mode { Mode::Old }\n")
        self.write("src/lib.rs", before)
        base = self.commit()
        self.write("src/lib.rs", before.replace("Old", "New"))
        _ = self.commit()
        self.assert_mechanical(base)

    def test_enum_variant_rename_ignores_names_owned_by_other_enums(self) -> None:
        before = ("enum Mode { Old, Other }\nenum Different { Old, New }\n"
                  "fn selected() -> Mode { Mode::Old }\n"
                  "fn unrelated() -> Different { Different::Old }\n")
        self.write("src/lib.rs", before)
        base = self.commit()
        self.write("src/lib.rs", before.replace("Mode { Old", "Mode { New").replace("Mode::Old", "Mode::New"))
        _ = self.commit()
        self.assert_mechanical(base)

    def test_enum_variant_rename_refuses_observing_attributes_and_derives(self) -> None:
        for attribute in ('#[cfg(feature = "mode")]', '#[serde(rename_all = "snake_case")]',
                          "#[derive(Reflect)]", "#[derive(Serialize)]", "#[derive(EnumString)]"):
            with self.subTest(attribute=attribute):
                before = attribute + "\nenum Mode { Old, Other }\nfn select() -> Mode { Mode::Old }\n"
                self.write("src/lib.rs", before)
                base = self.commit()
                self.write("src/lib.rs", before.replace("Old", "New"))
                _ = self.commit()
                _ = self.assert_review(base)

    def test_enum_variant_rename_refuses_enum_and_self_globs_in_another_file(self) -> None:
        for glob in ("use crate::Mode::*;", "use Self::*;"):
            with self.subTest(glob=glob):
                before = "enum Mode { Old, Other }\nfn select() -> Mode { Mode::Old }\n"
                self.write("src/lib.rs", "mod other;\n" + before)
                self.write("src/other.rs", glob + "\n")
                base = self.commit()
                self.write("src/lib.rs", "mod other;\n" + before.replace("Old", "New"))
                _ = self.commit()
                self.assertIn("check 6", self.assert_review(base))

    def test_enum_variant_rename_refuses_an_existing_variant_with_the_new_name(self) -> None:
        for declaration in ("enum Mode { Old, New }",
                            "#[derive(Default)]\nenum Mode { Old, #[default] New }"):
            with self.subTest(declaration=declaration):
                before = declaration + "\nfn select() -> Mode { Mode::Old }\n"
                self.write("src/lib.rs", before)
                base = self.commit()
                self.write("src/lib.rs", before.replace("Old", "New"))
                _ = self.commit()
                _ = self.assert_review(base)

    def test_enum_variant_rename_refuses_an_attribute_on_the_renamed_variant(self) -> None:
        before = ("#[derive(Default)]\nenum Mode { #[default] Old, Other }\n"
                  "fn select() -> Mode { Mode::Old }\n")
        self.write("src/lib.rs", before)
        base = self.commit()
        self.write("src/lib.rs", before.replace("Old", "New"))
        _ = self.commit()
        _ = self.assert_review(base)

    def test_enum_variant_rename_refuses_remaining_enum_or_self_uses(self) -> None:
        for remaining in ("fn select() -> crate::Mode { crate::Mode::Old }\n",
                          "impl crate::Mode { fn select() -> Self { Self::Old } }\n"):
            with self.subTest(remaining=remaining):
                self.write("src/lib.rs", "mod other;\nenum Mode { Old, Other }\n")
                self.write("src/other.rs", remaining)
                base = self.commit()
                self.write("src/lib.rs", "mod other;\nenum Mode { New, Other }\n")
                _ = self.commit()
                _ = self.assert_review(base)

    def test_enum_variant_rename_refuses_remaining_self_use_in_a_generic_impl(self) -> None:
        self.write("src/lib.rs", "mod other;\nenum Mode<T> { Old, Other(T) }\n")
        self.write("src/other.rs", "impl<T> crate::Mode<T> { fn select() -> Self { Self::Old } }\n")
        base = self.commit()
        self.write("src/lib.rs", "mod other;\nenum Mode<T> { New, Other(T) }\n")
        _ = self.commit()
        self.assertIn("check 6", self.assert_review(base))

    def test_enum_variant_rename_cannot_certify_another_enums_qualified_use(self) -> None:
        before = ("enum Mode { Old, Other }\nenum Different { Old, New }\n"
                  "fn select() -> Different { Different::Old }\n")
        self.write("src/lib.rs", before)
        base = self.commit()
        self.write("src/lib.rs", before.replace("Mode { Old", "Mode { New")
                   .replace("Different::Old", "Different::New"))
        _ = self.commit()
        self.assertIn("check 6", self.assert_review(base))

    def test_local_let_and_let_mut_renames_are_scoped_to_their_function(self) -> None:
        for mutable in ("", "mut "):
            with self.subTest(mutable=mutable):
                before = ("fn read(input: usize) -> usize {\n"
                          + f"    let {mutable}total = input;\n    total\n}}\n"
                          + "fn other(input: usize) -> usize { let total = input; total }\n"
                          + "fn already_sum(input: usize) -> usize { let sum = input; sum }\n")
                self.write("src/lib.rs", before)
                base = self.commit()
                self.write("src/lib.rs", before.replace("total", "sum", 2))
                _ = self.commit()
                self.assert_mechanical(base)

    def test_local_rename_refuses_a_name_already_used_in_the_function(self) -> None:
        before = ("fn read(input: usize, sum: usize) -> usize {\n"
                  "    let total = input;\n    total + sum\n}\n")
        self.write("src/lib.rs", before)
        base = self.commit()
        self.write("src/lib.rs", before.replace("total", "sum"))
        _ = self.commit()
        self.assertIn("check 6", self.assert_review(base))

    def test_local_rename_refuses_an_old_name_left_in_the_function(self) -> None:
        before = "fn read(input: usize) -> usize { let total = input; total }\n"
        self.write("src/lib.rs", before)
        base = self.commit()
        self.write("src/lib.rs", before.replace("let total", "let sum"))
        _ = self.commit()
        self.assertIn("check 6", self.assert_review(base))

    def test_local_rename_refuses_a_shorthand_field_inside_a_condition(self) -> None:
        before = ("use external::Point;\n"
                  "fn read(input: usize) { let x = input; if consume(Point { x }) {} }\n")
        self.write("src/lib.rs", before)
        base = self.commit()
        self.write("src/lib.rs", before.replace("let x", "let y").replace("Point { x }", "Point { y }"))
        _ = self.commit()
        self.assertIn("check 6", self.assert_review(base))

    def test_local_rename_cannot_certify_a_changed_parameter_in_another_function(self) -> None:
        before = ("fn read(input: usize) -> usize { let total = input; total }\n"
                  "fn other(total: usize) -> usize { total }\n")
        self.write("src/lib.rs", before)
        base = self.commit()
        self.write("src/lib.rs", before.replace("total", "sum"))
        _ = self.commit()
        self.assertIn("check 6", self.assert_review(base))

    def test_proven_local_rename_on_flagged_lines_is_mechanical(self) -> None:
        for expression in ("if total == 1 { total } else { 0 }", "match total { 0 => 1, _ => total }",
                           "{ while total < 3 { total += 1; } total }", "total + 1"):
            with self.subTest(expression=expression):
                before = ("fn read(input: usize) -> usize {\n"
                          + "    let mut total = input;\n    " + expression + "\n}\n")
                self.write("src/lib.rs", before)
                base = self.commit()
                self.write("src/lib.rs", before.replace("total", "sum"))
                _ = self.commit()
                self.assert_mechanical(base)

    def test_proven_import_rewrite_on_a_condition_line_is_mechanical(self) -> None:
        self.write("src/lib.rs", "mod columns;\nmod reader;\n")
        self.write("src/columns.rs", "pub(crate) const COL_DISK: usize = 1;\n")
        before = "use crate::columns;\nfn read() { if columns::COL_DISK == 1 { consume(1); } }\n"
        self.write("src/reader.rs", before)
        base = self.commit()
        self.write("src/reader.rs", "use crate::columns::COL_DISK;\n"
                   + "fn read() { if COL_DISK == 1 { consume(1); } }\n")
        _ = self.commit()
        self.assert_mechanical(base)

    def test_same_line_rename_and_literal_edit_requires_review(self) -> None:
        before = "fn read(input: usize) -> usize { let total = input; total + 1 }\n"
        self.write("src/lib.rs", before)
        base = self.commit()
        self.write("src/lib.rs", before.replace("total", "sum").replace("+ 1", "+ 2"))
        _ = self.commit()
        self.assertIn("check 6: condition, literal or control flow flagged", self.assert_review(base))

    def test_same_line_rename_and_condition_edit_requires_review(self) -> None:
        before = "fn read(input: usize) -> usize { let total = input; if total > 1 { total } else { 0 } }\n"
        self.write("src/lib.rs", before)
        base = self.commit()
        self.write("src/lib.rs", before.replace("total", "sum").replace("> 1", ">= 1"))
        _ = self.commit()
        self.assertIn("check 6: condition, literal or control flow flagged", self.assert_review(base))

    def test_local_rename_inside_a_macro_requires_review(self) -> None:
        before = "fn read(input: usize) { let total = input; assert_eq!(total, 1); }\n"
        self.write("src/lib.rs", before)
        base = self.commit()
        self.write("src/lib.rs", before.replace("total", "sum"))
        _ = self.commit()
        self.assertIn("check 6: macro invocation requires review", self.assert_review(base))

    def test_unequal_line_counts_keep_the_condition_line_flag(self) -> None:
        before = "fn read(input: usize) -> usize { let total = input; if total > 1 { total } else { 0 } }\n"
        self.write("src/lib.rs", before)
        base = self.commit()
        self.write("src/lib.rs", "fn read(input: usize) -> usize {\n"
                   + "    let sum = input; if sum > 1 { sum } else { 0 }\n}\n")
        _ = self.commit()
        self.assertIn("check 6: condition, literal or control flow flagged", self.assert_review(base))

    def test_proven_rename_on_a_condition_line_is_mechanical(self) -> None:
        before = (
            "fn read_value(input: usize) -> usize { input }\n"
            + "fn inspect(input: usize) { if read_value(input) == input {} }\n"
        )
        self.write("src/lib.rs", before)
        base = self.commit()
        self.write("src/lib.rs", before.replace("read_value", "load_value"))
        _ = self.commit()
        self.assert_mechanical(base)

    def test_reflected_struct_rename_cannot_be_proven_mechanical(self) -> None:
        before = "#[derive(Reflect)]\nstruct Value { input: usize }\n"
        self.write("src/lib.rs", before)
        base = self.commit()
        self.write("src/lib.rs", before.replace("Value", "Input"))
        _ = self.commit()
        self.assertIn("check 6: call target, method or other change not proven equivalent", self.assert_review(base))

    def test_local_binding_rename_cannot_also_rename_a_shorthand_field(self) -> None:
        before = (
            "use external::Point;\n"
            + "fn point(input: usize) -> Point { let x = input; Point { x } }\n"
        )
        self.write("src/lib.rs", before)
        base = self.commit()
        self.write("src/lib.rs", before.replace("let x", "let y").replace("Point { x }", "Point { y }"))
        _ = self.commit()
        self.assertIn("check 6: call target, method or other change not proven equivalent", self.assert_review(base))

    def test_local_definition_rename_cannot_certify_a_foreign_qualified_call(self) -> None:
        before = (
            "fn min(input: usize) -> usize { input }\n"
            + "fn pick(left: usize, right: usize) -> usize { std::cmp::min(left, right) }\n"
        )
        self.write("src/lib.rs", before)
        base = self.commit()
        self.write("src/lib.rs", before.replace("min", "max"))
        _ = self.commit()
        self.assertIn("check 6: call target, method or other change not proven equivalent", self.assert_review(base))

    def test_definition_finding_cannot_borrow_call_site_edits_from_another_finding(self) -> None:
        self.write("src/lib.rs", "mod first;\nfn read_value(input: usize) -> usize { input }\n")
        self.write("src/first.rs", "fn first(input: usize) -> usize { super::read_value(input) }\n")
        base = self.commit()
        for path in ("src/lib.rs", "src/first.rs"):
            self.write(path, (self.repo / path).read_text().replace("read_value", "load_value"))
        head = self.commit()
        self.write("findings.json", json.dumps({"definition": ["src/lib.rs"], "uses": ["src/first.rs"]}))
        result = self.check(base, head, "--finding-map", str(self.repo / "findings.json"))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(result.stdout.splitlines(), [
            "definition: review: check 6: call target, method or other change not proven equivalent",
            "uses: review: check 6: call target, method or other change not proven equivalent",
        ])

    def test_documented_call_change_inside_block_comment_requires_review(self) -> None:
        before = (
            "/**\n * Example usage:\n * Call read_value(input).\n */\n"
            + "fn read_value(input: usize) -> usize { input }\n"
            + "fn load_value(input: usize) -> usize { input }\n"
        )
        self.write("src/lib.rs", before)
        base = self.commit()
        self.write("src/lib.rs", before.replace(" * Call read_value(input).", " * Call load_value(input)."))
        _ = self.commit()
        self.assertEqual(self.assert_review(base).strip(),
                         "review: check 6: serialized, reflected, configured or documented name flagged")

    def test_definition_rename_cannot_certify_a_glob_import_call_in_another_file(self) -> None:
        self.write("src/lib.rs", "mod a;\nmod b;\n")
        self.write("src/a.rs", "pub(crate) fn min(input: usize) -> usize { input }\n")
        self.write("src/b.rs", "use std::cmp::*;\nfn pick(left: usize, right: usize) -> usize { min(left, right) }\n")
        base = self.commit()
        for path in ("src/a.rs", "src/b.rs"):
            self.write(path, (self.repo / path).read_text().replace("min", "max"))
        _ = self.commit()
        self.assertIn("check 6: call target, method or other change not proven equivalent", self.assert_review(base))

    def test_definition_rename_cannot_certify_a_glob_import_call_in_an_inline_module(self) -> None:
        before = (
            "fn min(input: usize) -> usize { input }\n"
            + "mod inner {\n    use std::cmp::*;\n"
            + "    fn pick(left: usize, right: usize) -> usize { min(left, right) }\n}\n"
        )
        self.write("src/lib.rs", before)
        base = self.commit()
        self.write("src/lib.rs", before.replace("min", "max"))
        _ = self.commit()
        self.assertIn("check 6: call target, method or other change not proven equivalent", self.assert_review(base))

    def test_glob_reexport_makes_a_private_file_modules_public_function_reachable(self) -> None:
        self.write("src/lib.rs", "mod internal;\npub use internal::*;\n")
        self.write("src/internal.rs", "pub fn read_value(input: usize) -> usize { input }\n")
        base = self.commit()
        self.write("src/internal.rs", "pub fn load_value(input: usize) -> usize { input }\n")
        _ = self.commit()
        self.assertEqual(self.assert_review(base).strip(), "review: check 2: externally reachable item changes")

    def test_glob_reexport_makes_a_private_inline_modules_public_function_reachable(self) -> None:
        before = (
            "mod internal {\n    pub fn read_value(input: usize) -> usize { input }\n}\n"
            + "pub use internal::*;\n"
        )
        self.write("src/lib.rs", before)
        base = self.commit()
        self.write("src/lib.rs", before.replace("read_value", "load_value"))
        _ = self.commit()
        self.assertIn("check 2: externally reachable item changes", self.assert_review(base))

    def test_private_rename_with_option_return_type_is_mechanical(self) -> None:
        before = (
            "fn read_value(input: Option<usize>) -> Option<usize> { input }\n"
            + "fn inspect(input: Option<usize>) -> Option<usize> { read_value(input) }\n"
        )
        self.write("src/lib.rs", before)
        base = self.commit()
        self.write("src/lib.rs", before.replace("read_value", "load_value"))
        _ = self.commit()
        result = self.check(base)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "mechanical\n", ""))

    def test_unrelated_test_attribute_does_not_prevent_private_rename(self) -> None:
        before = (
            "fn read_value(input: usize) -> usize { input }\n"
            + "fn inspect(input: usize) -> usize { read_value(input) }\n"
            + "#[cfg(test)]\nmod tests {}\n"
        )
        self.write("src/lib.rs", before)
        base = self.commit()
        self.write("src/lib.rs", before.replace("read_value", "load_value"))
        _ = self.commit()
        result = self.check(base)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "mechanical\n", ""))

    def test_added_derive_reports_a_trait_implementation_shape_change(self) -> None:
        self.write("src/lib.rs", "struct Value;\n")
        base = self.commit()
        self.write("src/lib.rs", "#[derive(Clone)]\nstruct Value;\n")
        _ = self.commit()
        self.assertIn("check 3: type, fields, variants, generics or trait impl changes", self.assert_review(base))

    def test_added_derive_on_a_union_reports_a_trait_implementation_shape_change(self) -> None:
        self.write("src/lib.rs", "union Value { input: usize }\n")
        base = self.commit()
        self.write("src/lib.rs", "#[derive(Copy, Clone)]\nunion Value { input: usize }\n")
        _ = self.commit()
        self.assertEqual(self.assert_review(base),
                         "review: check 3: type, fields, variants, generics or trait impl changes\n")

    def test_derive_on_an_unrecognized_item_is_not_erased_from_the_diff(self) -> None:
        self.write("src/lib.rs", "fn value(input: usize) -> usize { input }\n")
        base = self.commit()
        self.write("src/lib.rs", "#[derive(Clone)]\nfn value(input: usize) -> usize { input }\n")
        _ = self.commit()
        self.assertEqual(self.assert_review(base),
                         "review: check 6: call target, method or other change not proven equivalent\n")

    def test_merge_tree_tool_failure_reports_its_error_instead_of_a_merge_conflict(self) -> None:
        base = self.rename_fixture()
        _ = self.git("checkout", "-qb", "incoming")
        self.write("src/incoming.rs", "// Incoming work\n")
        _ = self.commit()
        _ = self.git("checkout", "main")
        _ = self.git("merge", "--no-ff", "-qm", "Merge incoming work", "incoming")
        head = self.rename_value()
        run = subprocess.run

        def fail_merge_tree(arguments: list[str], *, capture_output: bool, text: bool, check: bool,
                            input: str = "", errors: str = "strict") -> subprocess.CompletedProcess[str]:
            if arguments[3:5] == ["merge-tree", "--write-tree"]:
                return subprocess.CompletedProcess(arguments, 128, "", "fatal: merge-tree unavailable\n")
            return run(arguments, capture_output=capture_output, text=text, check=check, input=input, errors=errors)

        output = io.StringIO()
        with mock.patch("style_footprint.subprocess.run", side_effect=fail_merge_tree):
            with redirect_stdout(output):
                code = style_footprint.main(["check", "--repo", str(self.repo), "--base", base, "--head", head])
        self.assertEqual((code, output.getvalue()),
                         (1, "review: git merge-tree: fatal: merge-tree unavailable (exit 128)\n"))

    def test_incomplete_rename_with_old_uses_remaining_requires_review(self) -> None:
        base = self.rename_fixture()
        self.write("src/lib.rs", (self.repo / "src/lib.rs").read_text().replace("read_value", "load_value"))
        _ = self.commit()
        _ = self.assert_review(base)

    def test_merge_in_range_excludes_incoming_new_file_and_three_hundred_lines(self) -> None:
        base = self.rename_fixture()
        _ = self.git("checkout", "-qb", "incoming")
        self.write("src/incoming.rs", "// Incoming product work\n" * 300)
        _ = self.commit()
        _ = self.git("checkout", "main")
        _ = self.git("merge", "--no-ff", "-qm", "Merge incoming work", "incoming")
        head = self.rename_value()
        self.assert_mechanical(base, head)

    def test_multiple_merges_fold_each_incoming_parent_before_judging_rename(self) -> None:
        base = self.rename_fixture()
        for number in (1, 2):
            _ = self.git("checkout", "-qb", f"incoming-{number}")
            self.write(f"src/incoming{number}.rs", "// Incoming product work\n" * 300)
            _ = self.commit()
            _ = self.git("checkout", "main")
            _ = self.git("merge", "--no-ff", "-qm", "Merge incoming work", f"incoming-{number}")
        head = self.rename_value()
        self.assert_mechanical(base, head)

    def test_conflicting_base_fold_routes_to_merge_in_range_review(self) -> None:
        base = self.rename_fixture()
        _ = self.git("checkout", "-qb", "incoming-one")
        self.write("shared.txt", "first incoming version\n")
        _ = self.commit()
        _ = self.git("checkout", "-qb", "incoming-two", base)
        self.write("shared.txt", "second incoming version\n")
        _ = self.commit()
        _ = self.git("checkout", "main")
        _ = self.git("merge", "--no-ff", "-qm", "Merge first", "incoming-one")
        merge = subprocess.run(
            ["git", "-C", str(self.repo), "merge", "--no-ff", "--no-commit", "incoming-two"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(merge.returncode, 1, merge.stdout + merge.stderr)
        self.write("shared.txt", "resolved incoming versions\n")
        _ = self.commit()
        head = self.rename_value()
        self.assertIn("review: merge in range", self.assert_review(base, head))

    def test_literal_base_commit_survives_deletion_of_its_branch(self) -> None:
        base = self.rename_fixture()
        _ = self.git("branch", "ticket-start", base)
        head = self.rename_value()
        _ = self.git("branch", "-D", "ticket-start")
        self.assert_mechanical(base, head)

    def test_finding_map_reports_mechanical_rename_separately_from_behavior_change(self) -> None:
        _ = self.rename_fixture()
        self.write("src/behavior.rs", "fn value() -> usize { 1 }\n")
        base = self.commit()
        _ = self.rename_value()
        self.write("src/behavior.rs", "fn value() -> usize { 2 }\n")
        head = self.commit()
        self.write("findings.json", json.dumps({
            "rename": ["src/lib.rs", "src/first.rs", "src/second.rs"],
            "behavior": ["src/behavior.rs"],
        }))
        result = self.check(base, head, "--finding-map", str(self.repo / "findings.json"))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("rename: mechanical", result.stdout.splitlines())
        self.assertTrue(any(line.startswith("behavior: review:") for line in result.stdout.splitlines()), result.stdout)

    def test_finding_map_can_own_separate_hunks_in_the_same_file(self) -> None:
        before = (
            "fn read_value(input: usize) -> usize { input }\n"
            + "fn use_value(input: usize) -> usize { read_value(input) }\n\n"
            + "fn value() -> usize { 1 }\n"
        )
        self.write("src/lib.rs", before)
        base = self.commit()
        self.write("src/lib.rs", before.replace("read_value", "load_value").replace("{ 1 }", "{ 2 }"))
        head = self.commit()
        self.write("findings.json", json.dumps({
            "rename": {"hunks": [{"path": "src/lib.rs", "old_start": 1, "new_start": 1}]},
            "behavior": {"hunks": [{"path": "src/lib.rs", "old_start": 4, "new_start": 4}]},
        }))
        result = self.check(base, head, "--finding-map", str(self.repo / "findings.json"))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("rename: mechanical", result.stdout.splitlines())
        self.assertTrue(any(line.startswith("behavior: review:") for line in result.stdout.splitlines()), result.stdout)

    def test_finding_map_cannot_hide_unowned_changes(self) -> None:
        self.write("src/lib.rs", "fn read_value(input: usize) -> usize { input }\n")
        self.write("src/behavior.rs", "fn value() -> usize { 1 }\n")
        base = self.commit()
        self.write("src/lib.rs", "fn load_value(input: usize) -> usize { input }\n")
        self.write("src/behavior.rs", "fn value() -> usize { 2 }\n")
        head = self.commit()
        self.write("findings.json", json.dumps({"rename": ["src/lib.rs"]}))
        result = self.check(base, head, "--finding-map", str(self.repo / "findings.json"))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("review:", result.stdout)
        self.assertNotIn("rename: mechanical", result.stdout)

    def test_finding_map_rejects_two_findings_owning_the_same_hunk(self) -> None:
        base = self.rename_fixture()
        head = self.rename_value()
        self.write("findings.json", json.dumps({
            "first": ["src/lib.rs", "src/first.rs", "src/second.rs"],
            "second": ["src/lib.rs"],
        }))
        result = self.check(base, head, "--finding-map", str(self.repo / "findings.json"))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("review:", result.stdout)
