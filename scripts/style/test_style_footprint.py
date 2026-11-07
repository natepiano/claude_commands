"""Exercise the mechanical-fix boundary through committed temporary Git diffs."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import final, override
from unittest import mock

import style_footprint


SCRIPT = Path(__file__).with_name("style_footprint.py")


@final
class StyleFootprintTests(unittest.TestCase):
    repo: Path = Path()

    template_repo: Path = Path()

    @classmethod
    @override
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.template_repo = Path(cls.enterClassContext(tempfile.TemporaryDirectory(prefix="style-footprint-template-")))
        fixture = cls(methodName="runTest")
        fixture.repo = cls.template_repo
        _ = fixture.git("init", "-q", "--initial-branch=main")
        _ = fixture.git("config", "user.name", "Footprint tests")
        _ = fixture.git("config", "user.email", "footprint@example.invalid")
        _ = fixture.git("config", "commit.gpgsign", "false")
        fixture.write("Cargo.toml", '[package]\nname = "sample"\nversion = "0.1.0"\nedition = "2021"\n')

    @override
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="style-footprint-")))
        self.repo = root / "sample"
        _ = shutil.copytree(self.template_repo, self.repo, symlinks=True)

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

    def cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        output, errors = io.StringIO(), io.StringIO()
        code = 0
        with redirect_stdout(output), redirect_stderr(errors):
            try:
                code = style_footprint.main(list(arguments))
            except SystemExit as error:
                if isinstance(error.code, int):
                    code = error.code
                elif error.code is not None:
                    print(error.code, file=sys.stderr)
                    code = 1
        return subprocess.CompletedProcess([sys.executable, str(SCRIPT), *arguments], code,
                                           output.getvalue(), errors.getvalue())

    def check(self, base: str, head: str = "HEAD", *extra: str) -> subprocess.CompletedProcess[str]:
        return self.cli("check", "--repo", str(self.repo), "--base", base, "--head", head, *extra)

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
        return self.cli("check", "--repo", str(self.repo), "--base", base, "--worktree", *extra)

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
        self.assertEqual((code, output.getvalue()), (2, "error: snapshot failed\n"))
        self.assertTrue(indexes)
        self.assertTrue(all(not path.exists() for path in indexes))

    def test_missing_base_revision_is_an_error_not_a_review(self) -> None:
        self.write("src/lib.rs", "fn value() {}\n")
        _ = self.commit()
        result = self.check("0" * 40)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertTrue(result.stdout.startswith("error: "), result.stdout)

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

    def test_erasing_an_unused_required_trait_implementation_parameter_is_mechanical(self) -> None:
        before = ("trait Reader { fn read(&self, input: usize); }\n"
                  "struct Value;\nimpl Reader for Value { fn read(&self, _input: usize) {} }\n")
        self.write("src/lib.rs", before)
        base = self.commit()
        self.write("src/lib.rs", before.replace("_input: usize", "_: usize"))
        _ = self.commit()
        self.assert_mechanical(base)

    def test_erasing_a_bodyless_trait_method_parameter_is_mechanical(self) -> None:
        self.write("src/lib.rs", "trait Reader { fn read(&self, _input: usize); }\n")
        base = self.commit()
        self.write("src/lib.rs", "trait Reader { fn read(&self, _: usize); }\n")
        _ = self.commit()
        self.assert_mechanical(base)

    def test_erasing_an_unused_function_or_default_method_parameter_is_mechanical(self) -> None:
        for before in (
            "fn read(_input: usize) -> usize { 7 }\n",
            "trait Reader { fn read(&self, _input: usize) -> usize { 7 } }\n",
        ):
            with self.subTest(before=before):
                self.write("src/lib.rs", before)
                base = self.commit()
                self.write("src/lib.rs", before.replace("_input: usize", "_: usize"))
                _ = self.commit()
                self.assert_mechanical(base)

    def test_erasing_a_parameter_still_read_by_the_body_requires_review(self) -> None:
        for before in (
            "fn read(_input: usize) -> usize { _input }\n",
            "trait Reader { fn read(&self, _input: usize) -> usize { _input } }\n",
        ):
            with self.subTest(before=before):
                self.write("src/lib.rs", before)
                base = self.commit()
                self.write("src/lib.rs", before.replace("_input: usize", "_: usize"))
                _ = self.commit()
                self.assertIn("check 6", self.assert_review(base))

    def test_erasing_a_parameter_named_inside_a_macro_requires_review(self) -> None:
        before = "fn read(_input: usize) { consume!(_input); }\n"
        self.write("src/lib.rs", before)
        base = self.commit()
        self.write("src/lib.rs", before.replace("_input: usize", "_: usize"))
        _ = self.commit()
        self.assertIn("check 6", self.assert_review(base))

    def test_erasing_a_parameter_read_through_a_raw_identifier_requires_review(self) -> None:
        before = "fn read(_input: usize) -> usize { r#_input }\n"
        self.write("src/lib.rs", before)
        base = self.commit()
        self.write("src/lib.rs", before.replace("_input: usize", "_: usize"))
        _ = self.commit()
        self.assertIn("check 6", self.assert_review(base))

    def test_erasing_a_parameter_captured_through_literal_escapes_requires_review(self) -> None:
        for literal in (r'"{\u{5f}input}"', r'"{\x5finput}"'):
            with self.subTest(literal=literal):
                before = f"fn read(_input: usize) {{ println!({literal}); }}\n"
                self.write("src/lib.rs", before)
                base = self.commit()
                self.write("src/lib.rs", before.replace("_input: usize", "_: usize"))
                _ = self.commit()
                self.assertIn("check 6", self.assert_review(base))

    def test_erasing_a_parameter_with_unavailable_token_spans_requires_review(self) -> None:
        before = "fn read(_input: usize) { let café = 1; }\n"
        self.write("src/lib.rs", before)
        base = self.commit()
        self.write("src/lib.rs", before.replace("_input: usize", "_: usize"))
        _ = self.commit()
        self.assertIn("check 6", self.assert_review(base))

    def test_a_char_literal_with_a_unicode_or_byte_escape_is_one_literal(self) -> None:
        tokens = style_footprint.tokens(r"const BAR: char = '\u{2580}'; const TAB: u8 = b'\x7f';")
        self.assertEqual([token.value for token in tokens if token.kind == "literal"], [r"'\u{2580}'", r"b'\x7f'"])
        self.assertNotIn("unknown", {token.kind for token in tokens})

    def test_erasing_a_parameter_captured_by_a_plain_or_raw_format_literal_requires_review(self) -> None:
        for literal in ('"{_input}"', '"{_input:04}"', 'r#"{_input}"#', 'r#"{_input:04}"#'):
            with self.subTest(literal=literal):
                before = f"fn read(_input: usize) {{ println!({literal}); }}\n"
                self.write("src/lib.rs", before)
                base = self.commit()
                self.write("src/lib.rs", before.replace("_input: usize", "_: usize"))
                _ = self.commit()
                self.assertIn("check 6", self.assert_review(base))

    def test_erasing_an_unused_parameter_with_a_type_change_requires_review(self) -> None:
        self.write("src/lib.rs", "fn read(_input: usize) {}\n")
        base = self.commit()
        self.write("src/lib.rs", "fn read(_: u64) {}\n")
        _ = self.commit()
        self.assertIn("check 6", self.assert_review(base))

    def test_erasing_an_unused_externally_reachable_function_parameter_is_mechanical(self) -> None:
        self.write("src/lib.rs", "pub fn read(_input: usize) {}\n")
        base = self.commit()
        self.write("src/lib.rs", "pub fn read(_: usize) {}\n")
        _ = self.commit()
        self.assert_mechanical(base)

    def test_erasing_an_unused_public_trait_method_parameter_is_mechanical(self) -> None:
        for ending in (";", " {}"):
            with self.subTest(ending=ending):
                before = f"pub trait Reader {{ fn read(&self, _input: usize){ending} }}\n"
                self.write("src/lib.rs", before)
                base = self.commit()
                self.write("src/lib.rs", before.replace("_input: usize", "_: usize"))
                _ = self.commit()
                self.assert_mechanical(base)

    def test_parameter_erasure_keeps_review_for_another_public_signature_change(self) -> None:
        for before in (
            "pub fn read(_input: usize) -> usize { 7 }\n",
            "pub trait Reader { fn read(&self, _input: usize) -> usize { 7 } }\n",
        ):
            with self.subTest(before=before):
                self.write("src/lib.rs", before)
                base = self.commit()
                self.write("src/lib.rs", before.replace("_input: usize", "_: usize").replace("-> usize", "-> u64"))
                _ = self.commit()
                self.assertIn("check 2", self.assert_review(base))

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
                    self.assertEqual(
                        self.assert_review(base),
                        "review: check 5: src/lib.rs: 200 or more changed lines (size only)\n",
                    )

    def comment_fixture(self, modules: tuple[str, ...], lines: int) -> str:
        self.write("src/lib.rs", "".join(f"mod {module};\n" for module in modules))
        for module in modules:
            self.write(f"src/{module}.rs", "// Read the value.\n" * lines + "fn value() {}\n")
        base = self.commit()
        for module in modules:
            path = self.repo / f"src/{module}.rs"
            _ = path.write_text(path.read_text().replace("Read the value", "Load the value"))
        _ = self.commit()
        return base

    def test_changed_line_limit_counts_each_file_not_the_whole_diff(self) -> None:
        base = self.comment_fixture(tuple(f"part{index}" for index in range(19)), 8)
        self.assert_mechanical(base)

    def test_each_file_over_the_changed_line_limit_gives_its_own_reason(self) -> None:
        base = self.comment_fixture(("first", "second"), 100)
        self.assertEqual(self.assert_review(base), (
            "review: check 5: src/first.rs: 200 or more changed lines (size only); "
            "check 5: src/second.rs: 200 or more changed lines (size only)\n"
        ))

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

    SERIALIZED_REASON = "check 6: serialized, reflected, configured or documented name flagged"

    def test_a_serde_or_reflect_attribute_a_comment_or_raw_word_hides_requires_review(self) -> None:
        body = "struct Value {\n    input: usize,\n}\n"
        for attribute in (
            "#/* c */[serde(default)]", "# // c\n    [serde(default)]", "#[r#serde(default)]",
            '#[cfg_attr(feature = "s", serde(skip))]', "#[reflect(ignore)]", '#[custom(rename // c\n    = "x")]',
        ):
            with self.subTest(attribute=attribute):
                self.write("src/lib.rs", body)
                base = self.commit()
                self.write("src/lib.rs", body.replace("    input", f"    {attribute}\n    input"))
                _ = self.commit()
                self.assertIn(self.SERIALIZED_REASON, self.assert_review(base))

    def test_an_inner_reflect_attribute_requires_review(self) -> None:
        body = "struct Value;\n"
        self.write("src/lib.rs", body)
        base = self.commit()
        self.write("src/lib.rs", "#![reflect(ignore)]\n" + body)
        _ = self.commit()
        self.assertIn(self.SERIALIZED_REASON, self.assert_review(base))

    def test_a_serde_word_on_a_changed_line_of_a_longer_attribute_requires_review(self) -> None:
        before = '#[cfg_attr(\n    feature = "s",\n    serde(default)\n)]\nstruct Value;\n'
        self.write("src/lib.rs", before)
        base = self.commit()
        self.write("src/lib.rs", before.replace("serde(default)", "serde(skip)"))
        _ = self.commit()
        self.assertIn(self.SERIALIZED_REASON, self.assert_review(base))

    def test_a_serde_attribute_in_a_comment_or_a_string_is_not_a_serialized_name(self) -> None:
        for before, after in (
            ("fn value() {}\n", "// #[serde(x)]\nfn value() {}\n"),
            ('fn value() -> &\'static str { "a" }\n', 'fn value() -> &\'static str { "#[serde]" }\n'),
        ):
            with self.subTest(after=after):
                self.write("src/lib.rs", before)
                base = self.commit()
                self.write("src/lib.rs", after)
                _ = self.commit()
                self.assertNotIn(self.SERIALIZED_REASON, self.check(base).stdout)

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

    def shorten_module_constants(self, module: str, extra: str) -> str:
        self.write("src/lib.rs", f"mod {module};\nmod card;\n")
        self.write(f"src/{module}.rs", 'pub(crate) const VERTICAL: &str = "|";\n'
                   + 'pub(crate) const VERTICAL_RIGHT: &str = "|-";\n')
        body = ("fn fade(text: &str) -> &str { text }\nfn set_symbol(_symbol: &str) {}\n"
                + f"fn paint(symbol: &str) {{\n    if symbol == {module}::VERTICAL_RIGHT {{\n"
                + f"        set_symbol({module}::VERTICAL);\n    }}\n}}\n" + extra)
        self.write("src/card.rs", f"use crate::{module};\n" + body)
        base = self.commit()
        self.write("src/card.rs", f"use crate::{module}::VERTICAL;\nuse crate::{module}::VERTICAL_RIGHT;\n"
                   + body.replace(f"{module}::VERTICAL", "VERTICAL"))
        _ = self.commit()
        return base

    def test_constant_import_shortening_allows_a_removed_module_name_as_a_closure_binding(self) -> None:
        base = self.shorten_module_constants(
            "line", "fn widths(rows: &[&str]) -> usize { rows.iter().flat_map(|line| { fade(line).chars() }).count() }\n",
        )
        self.assert_mechanical(base)

    def test_constant_import_shortening_with_a_removed_module_name_and_a_glob_requires_review(self) -> None:
        base = self.shorten_module_constants("line", "use std::collections::*;\nfn count(line: usize) -> usize { line }\n")
        self.assertIn("check 6", self.assert_review(base))

    def test_constant_import_shortening_with_a_removed_prelude_module_name_requires_review(self) -> None:
        for module, extra in (("drop", "fn release(value: String) { drop(value) }\n"),
                              ("Option", "fn pick() -> Option<u32> { None }\n")):
            with self.subTest(module=module):
                base = self.shorten_module_constants(module, extra)
                self.assertIn("check 6", self.assert_review(base))

    def test_constant_import_shortening_with_a_removed_module_name_as_a_macro_requires_review(self) -> None:
        base = self.shorten_module_constants("line", "fn here() -> u32 { line!() }\n")
        self.assertIn("check 6", self.assert_review(base))

    def test_constant_import_shortening_with_a_removed_module_path_left_requires_review(self) -> None:
        base = self.shorten_module_constants("line", "fn other() -> &'static str { line::OTHER }\n")
        self.assertIn("check 6", self.assert_review(base))

    def test_constant_import_shortening_with_a_removed_module_name_in_a_macro_argument_requires_review(self) -> None:
        base = self.shorten_module_constants("line", "fn here() { invoke!(line) }\n")
        self.assertIn("check 6", self.assert_review(base))

    def test_constant_import_shortening_with_a_removed_module_name_in_a_macro_rules_body_requires_review(self) -> None:
        base = self.shorten_module_constants("line", "macro_rules! paint_line { () => { line }; }\n")
        self.assertIn("check 6", self.assert_review(base))

    def test_constant_import_shortening_with_a_removed_constant_left_as_a_pattern_requires_review(self) -> None:
        self.write("src/lib.rs", "mod symbols;\nmod card;\n")
        # A C binding crate exports snake-case constants such as `err_none`.
        for constant in ("LIMIT", "err_none"):
            with self.subTest(constant=constant):
                self.write("src/symbols.rs", f"pub(crate) const {constant}: usize = 4;\n"
                           + 'pub(crate) const VERTICAL: &str = "|";\n')
                body = ("fn paint(symbol: &str) {\n    if symbol == symbols::VERTICAL {}\n}\n"
                        + f"fn full(x: usize) -> bool {{ match x {{ {constant} => true, _ => false }} }}\n")
                self.write("src/card.rs", f"use crate::symbols;\nuse crate::symbols::{constant};\n" + body)
                base = self.commit()
                self.write("src/card.rs", "use crate::symbols;\nuse crate::symbols::VERTICAL;\n"
                           + body.replace("symbols::VERTICAL", "VERTICAL"))
                _ = self.commit()
                self.assertIn("check 6", self.assert_review(base))

    def write_scope_definitions(self) -> None:
        self.write("src/lib.rs", "mod c;\nmod shapes;\nmod card;\n")
        self.write("src/c.rs", "pub(crate) const err_none: usize = 0;\n")
        self.write("src/shapes.rs", 'pub(crate) mod err_none { pub(crate) const VERTICAL: &str = "|"; }\n'
                   + 'pub(crate) mod line { pub(crate) const VERTICAL: &str = "|"; }\n')

    def test_constant_import_shortening_with_a_child_import_in_another_module_requires_review(self) -> None:
        self.write_scope_definitions()
        query = ("mod ffi {\n    pub(crate) use crate::c::err_none;\n    mod query {\n        use super::err_none;\n"
                 + "        fn ok(x: usize) -> bool { match x { err_none => true, _ => false } }\n    }\n}\n")
        paint = ("mod ui {\n    pub(crate) use crate::shapes::err_none;\n    mod paint {\n        use super::err_none;\n"
                 + "        fn paint(symbol: &str) { if symbol == err_none::VERTICAL {} }\n    }\n}\n")
        self.write("src/card.rs", query + paint)
        base = self.commit()
        self.write("src/card.rs", query.replace("        use super::err_none;\n", "") + paint.replace(
            "use super::err_none;", "use super::err_none::VERTICAL;").replace("err_none::VERTICAL {", "VERTICAL {"))
        _ = self.commit()
        self.assertIn("check 6", self.assert_review(base))

    def test_constant_import_shortening_with_a_removed_import_kept_elsewhere_requires_review(self) -> None:
        self.write_scope_definitions()
        paint = ("mod ui {\n    pub(crate) use crate::shapes::line;\n    mod paint {\n        use super::line;\n"
                 + "        fn paint(symbol: &str) { if symbol == line::VERTICAL {} }\n    }\n}\n")
        for kept in ("    }\n    mod check {\n        use super::err_none;\n        fn code() -> usize { err_none }\n",
                     # The braces keep the block-level use from pairing with the removed module-level one.
                     "        fn code() -> usize { use super::{err_none}; err_none }\n"):
            with self.subTest(kept=kept):
                query = ("mod ffi {\n    pub(crate) use crate::c::err_none;\n    mod query {\n        use super::err_none;\n"
                         + "        fn ok(x: usize) -> bool { match x { err_none => true, _ => false } }\n"
                         + kept + "    }\n}\n")
                self.write("src/card.rs", query + paint)
                base = self.commit()
                self.write("src/card.rs", query.replace("        use super::err_none;\n", "", 1) + paint.replace(
                    "use super::line;", "use super::line::VERTICAL;").replace("line::VERTICAL {", "VERTICAL {"))
                _ = self.commit()
                self.assertIn("check 6", self.assert_review(base))

    def test_constant_import_shortening_with_a_removed_block_import_requires_review(self) -> None:
        self.write_scope_definitions()
        before = ("mod provider { pub(crate) const err_none: u32 = 0; }\nuse provider::err_none;\n"
                  + "fn ok(x: u32) -> bool {\n    mod provider { pub const err_none: u32 = 1; }\n"
                  + "    use provider::err_none;\n    match x { err_none => true, _ => false }\n}\n"
                  + "use crate::shapes::line;\nfn paint(symbol: &str) { if symbol == line::VERTICAL {} }\n")
        self.write("src/card.rs", before)
        base = self.commit()
        self.write("src/card.rs", before.replace("    use provider::err_none;\n", "").replace(
            "use crate::shapes::line;", "use crate::shapes::line::VERTICAL;").replace("line::VERTICAL {", "VERTICAL {"))
        _ = self.commit()
        self.assertIn("check 6", self.assert_review(base))

    def test_constant_import_shortening_with_a_removed_module_name_in_an_attribute_requires_review(self) -> None:
        for attribute in ("#[line]", "#[line(fast)]"):
            with self.subTest(attribute=attribute):
                base = self.shorten_module_constants("line", f"{attribute}\nfn tagged() {{}}\n")
                self.assertIn("check 6", self.assert_review(base))

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

    def test_import_rewrite_with_joined_calls_that_drop_trailing_commas_is_mechanical(self) -> None:
        self.write("src/lib.rs", "mod constants;\nmod error;\n")
        self.write("src/constants.rs", "".join(f"pub(super) const {name}: &str = \"{name}\";\n" for name in (
            "GIT_PATHSPEC_SEPARATOR", "LSOF_BINARY", "LSOF_PROCESS_IDS_ARG", "PROCESS_DESCRIPTORS_DIRECTORY",
            "PROCESS_DIRECTORY")))
        before = ("use std::path::Path;\n\n"
                  "#[cfg(target_os = \"linux\")]\n"
                  "fn git_lock_holders(path: &Path) -> u32 {\n"
                  "    inspect_git_lock_holders(\n"
                  "        Path::new(super::constants::PROCESS_DIRECTORY),\n"
                  "        path,\n"
                  "        metadata.uid(),\n"
                  "    )\n"
                  "}\n\n"
                  "#[cfg(target_os = \"linux\")]\n"
                  "fn inspect_git_lock_holders(root: &Path, path: &Path, owner: u32) -> u32 {\n"
                  "    let Ok(descriptors) = std::fs::read_dir(\n"
                  "        root\n"
                  "            .join(path)\n"
                  "            .join(super::constants::PROCESS_DESCRIPTORS_DIRECTORY),\n"
                  "    ) else {\n"
                  "        return owner;\n"
                  "    };\n"
                  "    consume(descriptors)\n"
                  "}\n\n"
                  "#[cfg(not(target_os = \"linux\"))]\n"
                  "fn git_lock_holders(path: &Path) -> u32 {\n"
                  "    let Ok(output) = std::process::Command::new(super::constants::LSOF_BINARY)\n"
                  "        .args([\n"
                  "            super::constants::LSOF_PROCESS_IDS_ARG,\n"
                  "            super::constants::GIT_PATHSPEC_SEPARATOR,\n"
                  "        ])\n"
                  "        .arg(path)\n"
                  "        .output()\n"
                  "    else {\n"
                  "        return owner;\n"
                  "    };\n"
                  "    consume(output)\n"
                  "}\n")
        self.write("src/error.rs", before)
        base = self.commit()
        imports = ("use std::path::Path;\n\n"
                   "#[cfg(not(target_os = \"linux\"))]\nuse super::constants::GIT_PATHSPEC_SEPARATOR;\n"
                   "#[cfg(not(target_os = \"linux\"))]\nuse super::constants::LSOF_BINARY;\n"
                   "#[cfg(not(target_os = \"linux\"))]\nuse super::constants::LSOF_PROCESS_IDS_ARG;\n"
                   "#[cfg(target_os = \"linux\")]\nuse super::constants::PROCESS_DESCRIPTORS_DIRECTORY;\n"
                   "#[cfg(target_os = \"linux\")]\nuse super::constants::PROCESS_DIRECTORY;\n")
        joins = (
            (("inspect_git_lock_holders(\n        Path::new(super::constants::PROCESS_DIRECTORY),\n"
              "        path,\n        metadata.uid(),\n    )"),
             "inspect_git_lock_holders(Path::new(PROCESS_DIRECTORY), path, metadata.uid())"),
            (("std::fs::read_dir(\n        root\n            .join(path)\n"
              "            .join(super::constants::PROCESS_DESCRIPTORS_DIRECTORY),\n    ) else {"),
             "std::fs::read_dir(root.join(path).join(PROCESS_DESCRIPTORS_DIRECTORY))\n    else {"),
            ("super::constants::LSOF_BINARY", "LSOF_BINARY"),
            ((".args([\n            super::constants::LSOF_PROCESS_IDS_ARG,\n"
              "            super::constants::GIT_PATHSPEC_SEPARATOR,\n        ])"),
             ".args([LSOF_PROCESS_IDS_ARG, GIT_PATHSPEC_SEPARATOR])"),
        )
        body = before[len("use std::path::Path;\n"):]
        for old, new in joins:
            self.assertIn(old, body)
            body = body.replace(old, new)
        self.write("src/error.rs", imports + body)
        _ = self.commit()
        self.assert_mechanical(base)

    def test_each_joined_call_that_drops_its_trailing_comma_is_mechanical(self) -> None:
        cases = (
            ("    consume(\n        columns::COL_DISK,\n        input,\n    );\n", "    consume(COL_DISK, input);\n"),
            (("    reader\n        .args([\n            columns::COL_DISK,\n            input,\n        ])\n"
              "        .output();\n"), "    reader.args([COL_DISK, input]).output();\n"),
            ("    let Ok(value) = read(\n        columns::COL_DISK,\n    ) else {\n        return;\n    };\n",
             "    let Ok(value) = read(COL_DISK) else {\n        return;\n    };\n"),
        )
        for body, joined in cases:
            with self.subTest(joined=joined):
                self.write("src/lib.rs", "mod columns;\nmod reader;\n")
                self.write("src/columns.rs", "pub(crate) const COL_DISK: usize = 1;\n")
                self.write("src/reader.rs", "use crate::columns;\nfn read(input: usize) {\n" + body + "}\n")
                base = self.commit()
                self.write("src/reader.rs", "use crate::columns::COL_DISK;\nfn read(input: usize) {\n" + joined + "}\n")
                _ = self.commit()
                self.assert_mechanical(base)

    def test_a_dropped_comma_in_a_tuple_macro_or_attribute_requires_review(self) -> None:
        cases = (
            ("fn read(x: u8) { let t = (x,); consume(t); }\n", "(x,)", "(x)"),
            ("fn read(v: (u8,)) { let (a,) = v; consume(a); }\n", "(a,)", "(a)"),
            ("fn read(x: u8) -> (u8,) { make(x) }\n", "(u8,)", "(u8)"),
            ("fn read(a: u8, b: u8) { assert_eq!(a, b,); }\n", "b,)", "b)"),
            ("#[cfg(any(unix, windows,))]\nfn read() {}\n", "windows,)", "windows)"),
        )
        for before, old, new in cases:
            with self.subTest(before=before):
                self.write("src/lib.rs", before)
                base = self.commit()
                self.write("src/lib.rs", before.replace(old, new))
                _ = self.commit()
                self.assertIn("check 6", self.assert_review(base))

    def test_an_added_trailing_comma_is_mechanical(self) -> None:
        # The check reads no edition, and before 2024 `gen` is a name, so `gen(input)` is a call.
        for call in ("consume", "gen"):
            with self.subTest(call=call):
                before = f"fn read(input: usize) {{\n    {call}(input);\n}}\n"
                self.write("src/lib.rs", before)
                base = self.commit()
                self.write("src/lib.rs", before.replace(f"{call}(input);", f"{call}(\n        input,\n    );"))
                _ = self.commit()
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
        # A string in a macro call does not end the `use` the macro reads.
        macro = "macro_rules! open {\n    (use $note:literal $($path:tt)*) => { use $($path)*; };\n}\n"
        for lib, glob in (("", "use std::cmp::*;\n"), (macro, 'open!(use "a;" std::cmp::*);\n')):
            with self.subTest(glob=glob):
                self.write("src/lib.rs", lib + "mod a;\nmod b;\n")
                self.write("src/a.rs", "pub(crate) fn min(input: usize) -> usize { input }\n")
                self.write("src/b.rs", glob + "fn pick(left: usize, right: usize) -> usize { min(left, right) }\n")
                base = self.commit()
                for path in ("src/a.rs", "src/b.rs"):
                    self.write(path, (self.repo / path).read_text().replace("min", "max"))
                _ = self.commit()
                self.assertIn("check 6: call target, method or other change not proven equivalent",
                              self.assert_review(base))

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
                         (2, "error: git merge-tree: fatal: merge-tree unavailable (exit 128)\n"))

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

    LIMIT_FILES: dict[str, str] = {
        "src/lib.rs": "mod constants;\nmod reader;\nmod writer;\n",
        "src/constants.rs": "pub(crate) const OTHER: usize = 1;\n",
        "src/writer.rs": "/// The most rows one write takes.\npub(crate) const LIMIT: usize = 4;\n\n"
                         + "pub(crate) fn write() -> usize { LIMIT + 1 }\n",
        "src/reader.rs": "use crate::writer::LIMIT;\n\npub(crate) fn read() -> usize { LIMIT }\n",
    }
    LIMIT_MOVED: dict[str, str] = {
        "src/constants.rs": "pub(crate) const OTHER: usize = 1;\n\n/// The most rows one write takes.\n"
                            + "pub(crate) const LIMIT: usize = 4;\n",
        "src/writer.rs": "use crate::constants::LIMIT;\n\npub(crate) fn write() -> usize { LIMIT + 1 }\n",
        "src/reader.rs": "use crate::constants::LIMIT;\n\npub(crate) fn read() -> usize { LIMIT }\n",
    }

    def prove_move(self, before: dict[str, str], after: dict[str, str]
                   ) -> style_footprint.ItemMoves | style_footprint.NoItemMoves:
        for path, text in before.items():
            self.write(path, text)
        base = self.commit()
        for path, text in after.items():
            self.write(path, text)
        head = self.commit()
        changes = style_footprint.changes_between(self.repo, base, head)
        return style_footprint.prove_item_moves(changes, style_footprint.RevisionTree(self.repo, base))

    def assert_move_refused(self, reason: str, before: dict[str, str], after: dict[str, str]) -> None:
        result = self.prove_move(before, after)
        self.assertIsInstance(result, style_footprint.NoItemMoves, result)
        assert isinstance(result, style_footprint.NoItemMoves)
        self.assertIn(reason, result.reason)

    def test_a_const_moved_to_an_existing_constants_module_is_proven(self) -> None:
        result = self.prove_move(self.LIMIT_FILES, self.LIMIT_MOVED)
        assert isinstance(result, style_footprint.ItemMoves), result
        self.assertEqual([(move.kind, move.name) for move in result.moves], [("const", "LIMIT")])
        self.assertEqual(len(result.sites), 2)
        self.assertEqual({site.text for site in result.sites}, {"crate::constants"})

    def test_the_session_environment_const_moves_to_constants_to_end_an_import_loop(self) -> None:
        before = {
            "src/lib.rs": "mod berth_command;\nmod constants;\nmod git_driver;\n\n"
                          + "pub use berth_command::CLAUDE_CODE_SESSION_ENVIRONMENT;\npub use berth_command::berth_command;\n"
                          + "pub use constants::DEFAULT_TIMEOUT;\n",
            "src/constants.rs": "/// How long a berth command waits, in seconds.\npub const DEFAULT_TIMEOUT: u64 = 30;\n",
            "src/berth_command.rs": (
                "use std::process::Command;\n\n"
                + "/// The environment variable that holds the Claude Code session id.\n"
                + "pub const CLAUDE_CODE_SESSION_ENVIRONMENT: &str = \"CLAUDE_CODE_SESSION_ID\";\n\n"
                + "pub fn berth_command() -> Command {\n    let mut command = Command::new(\"cargo-berth\");\n"
                + "    command.env_remove(CLAUDE_CODE_SESSION_ENVIRONMENT);\n"
                + "    crate::git_driver::disable_git_auto_maintenance(&mut command);\n    command\n}\n"),
            "src/git_driver.rs": (
                "use std::process::Command;\n\nuse crate::berth_command::CLAUDE_CODE_SESSION_ENVIRONMENT;\n\n"
                + "pub fn disable_git_auto_maintenance(command: &mut Command) {\n"
                + "    command.env(\"GIT_CONFIG_PARAMETERS\", \"'maintenance.auto=false'\");\n"
                + "    command.env_remove(CLAUDE_CODE_SESSION_ENVIRONMENT);\n}\n"),
        }
        after = {
            "src/lib.rs": before["src/lib.rs"].replace("pub use berth_command::CLAUDE_CODE_SESSION_ENVIRONMENT;\n", "")
                          .replace("pub use constants::DEFAULT_TIMEOUT;\n",
                                   "pub use constants::CLAUDE_CODE_SESSION_ENVIRONMENT;\npub use constants::DEFAULT_TIMEOUT;\n"),
            "src/constants.rs": before["src/constants.rs"]
                                + "\n/// The environment variable that holds the Claude Code session id.\n"
                                  + "pub const CLAUDE_CODE_SESSION_ENVIRONMENT: &str = \"CLAUDE_CODE_SESSION_ID\";\n",
            "src/berth_command.rs": before["src/berth_command.rs"].replace(
                "/// The environment variable that holds the Claude Code session id.\n"
                + "pub const CLAUDE_CODE_SESSION_ENVIRONMENT: &str = \"CLAUDE_CODE_SESSION_ID\";\n",
                "use crate::constants::CLAUDE_CODE_SESSION_ENVIRONMENT;\n"),
            "src/git_driver.rs": before["src/git_driver.rs"].replace("crate::berth_command::", "crate::constants::"),
        }
        result = self.prove_move(before, after)
        assert isinstance(result, style_footprint.ItemMoves), result
        [move] = result.moves
        source = before["src/berth_command.rs"][move.source.start:move.source.end]
        self.assertTrue(source.startswith("/// The environment variable"), source)
        self.assertTrue(source.endswith('"CLAUDE_CODE_SESSION_ID";'), source)
        self.assertIsNotNone(move.source_use)
        self.assertEqual({site.path for site in result.sites}, {"src/lib.rs", "src/git_driver.rs"})

    def test_a_const_moved_to_a_new_constants_module_with_its_mod_line_is_proven(self) -> None:
        before = {**self.LIMIT_FILES, "src/lib.rs": "mod reader;\nmod writer;\n"}
        del before["src/constants.rs"]
        after = {**self.LIMIT_MOVED, "src/lib.rs": "mod constants;\nmod reader;\nmod writer;\n",
                 "src/constants.rs": "/// The most rows one write takes.\npub(crate) const LIMIT: usize = 4;\n"}
        result = self.prove_move(before, after)
        assert isinstance(result, style_footprint.ItemMoves), result
        [line] = result.module_lines
        self.assertEqual(after["src/lib.rs"][line.start:line.end], "mod constants;")

    PUBLIC_FILES: dict[str, str] = {
        "src/lib.rs": "mod caller;\nmod format;\npub mod report;\n",
        "src/format.rs": "pub(crate) fn width() -> usize { 8 }\n",
        "src/report.rs": "/// Render the report.\npub fn render() -> usize { 3 }\n\npub fn total() -> usize { render() }\n",
        "src/caller.rs": "pub(crate) fn call() -> usize { crate::report::render() }\n",
    }

    def test_a_public_function_moved_with_a_public_reexport_is_proven(self) -> None:
        after = {
            "src/format.rs": "pub(crate) fn width() -> usize { 8 }\n\n/// Render the report.\npub fn render() -> usize { 3 }\n",
            "src/report.rs": "pub use crate::format::render;\n\npub fn total() -> usize { render() }\n",
        }
        result = self.prove_move(self.PUBLIC_FILES, after)
        assert isinstance(result, style_footprint.ItemMoves), result
        self.assertEqual([(move.kind, move.name) for move in result.moves], [("fn", "render")])

    def test_an_impl_moved_to_its_type_module_is_proven(self) -> None:
        before = {
            "src/lib.rs": "mod draw;\nmod shape;\n",
            "src/shape.rs": "pub(crate) struct Shape {\n    pub(crate) width: f64,\n    pub(crate) height: f64,\n}\n",
            "src/draw.rs": "use crate::shape::Shape;\n\nimpl Shape {\n"
                           + "    pub(crate) fn area(&self) -> f64 { self.width * self.height }\n}\n\n"
                           + "pub(crate) fn draw(shape: &Shape) -> f64 { shape.area() }\n",
        }
        after = {
            "src/shape.rs": before["src/shape.rs"]
                            + "\nimpl Shape {\n    pub(crate) fn area(&self) -> f64 { self.width * self.height }\n}\n",
            "src/draw.rs": "use crate::shape::Shape;\n\npub(crate) fn draw(shape: &Shape) -> f64 { shape.area() }\n",
        }
        result = self.prove_move(before, after)
        assert isinstance(result, style_footprint.ItemMoves), result
        self.assertEqual([(move.kind, move.name) for move in result.moves], [("impl", "")])

    def test_an_item_move_is_refused_when_the_item_changes(self) -> None:
        after = {**self.LIMIT_MOVED, "src/constants.rs": self.LIMIT_MOVED["src/constants.rs"].replace("= 4", "= 5")}
        self.assert_move_refused("changes during the move", self.LIMIT_FILES, after)

    def test_an_item_move_across_crates_is_refused(self) -> None:
        before = {**self.LIMIT_FILES, "crates/other/Cargo.toml": '[package]\nname = "other"\nversion = "0.1.0"\n',
                  "crates/other/src/lib.rs": "pub(crate) const OTHER: usize = 1;\n"}
        after = {**self.LIMIT_MOVED, "crates/other/src/lib.rs": self.LIMIT_MOVED["src/constants.rs"]}
        del after["src/constants.rs"]
        self.assert_move_refused("crosses crates", before, after)

    def test_an_item_move_is_refused_when_the_destination_imports_from_the_source(self) -> None:
        before = {**self.LIMIT_FILES, "src/writer.rs": "pub(crate) const BASE: usize = 2;\n\n"
                                                       + "/// The most rows one write takes.\n"
                                                       + "pub(crate) const LIMIT: usize = BASE * 2;\n\n"
                                                       + "pub(crate) fn write() -> usize { LIMIT + 1 }\n"}
        after = {**self.LIMIT_MOVED,
                 "src/constants.rs": "use crate::writer::BASE;\n\npub(crate) const OTHER: usize = 1;\n\n"
                                     + "/// The most rows one write takes.\npub(crate) const LIMIT: usize = BASE * 2;\n",
                 "src/writer.rs": "use crate::constants::LIMIT;\n\npub(crate) const BASE: usize = 2;\n\n"
                                  + "pub(crate) fn write() -> usize { LIMIT + 1 }\n"}
        self.assert_move_refused("an import cycle", before, after)

    def test_a_public_function_move_is_refused_when_the_reexport_is_missing(self) -> None:
        before = {**self.PUBLIC_FILES, "src/lib.rs": "mod caller;\nmod format;\nmod report;\n"}
        after = {
            "src/format.rs": "pub(crate) fn width() -> usize { 8 }\n\n/// Render the report.\npub fn render() -> usize { 3 }\n",
            "src/report.rs": "use crate::format::render;\n\npub fn total() -> usize { render() }\n",
        }
        self.assert_move_refused("render leaves src/report.rs without a re-export, and src/caller.rs still uses its old "
                                 + "path", before, after)

    def test_a_public_function_move_out_of_a_public_module_is_refused_without_a_reexport(self) -> None:
        after = {
            "src/format.rs": "pub(crate) fn width() -> usize { 8 }\n\n/// Render the report.\npub fn render() -> usize { 3 }\n",
            "src/report.rs": "use crate::format::render;\n\npub fn total() -> usize { render() }\n",
            "src/caller.rs": "pub(crate) fn call() -> usize { crate::format::render() }\n",
        }
        self.assert_move_refused("other crates can reach src/report.rs", self.PUBLIC_FILES, after)

    def test_an_item_move_is_refused_when_the_reexport_widens_its_visibility(self) -> None:
        after = {**self.LIMIT_MOVED, "src/writer.rs": self.LIMIT_MOVED["src/writer.rs"].replace("use ", "pub use ")}
        self.assert_move_refused("the re-export of LIMIT in src/writer.rs is `pub`", self.LIMIT_FILES, after)

    def test_an_item_move_is_refused_when_a_touched_file_has_a_glob_use(self) -> None:
        after = {**self.LIMIT_MOVED, "src/reader.rs": "use crate::constants::*;\n\npub(crate) fn read() -> usize { LIMIT }\n"}
        self.assert_move_refused("src/reader.rs: a glob use of crate::constants, a module the move changes",
                                 self.LIMIT_FILES, after)

    def test_a_trait_move_is_refused(self) -> None:
        trait = "\npub(crate) trait Limited {\n    fn limit(&self) -> usize;\n}\n"
        before = {**self.LIMIT_FILES, "src/writer.rs": self.LIMIT_FILES["src/writer.rs"] + trait}
        after = {**self.LIMIT_MOVED, "src/constants.rs": self.LIMIT_MOVED["src/constants.rs"] + trait}
        self.assert_move_refused("a trait moves: trait Limited", before, after)

    def test_an_item_move_is_refused_when_the_destination_gains_another_item(self) -> None:
        after = {**self.LIMIT_MOVED,
                 "src/constants.rs": self.LIMIT_MOVED["src/constants.rs"] + "\npub(crate) const EXTRA: usize = 9;\n"}
        self.assert_move_refused("src/constants.rs gains another item: const EXTRA", self.LIMIT_FILES, after)

    def test_an_item_move_is_refused_when_the_source_keeps_a_copy(self) -> None:
        after = {**self.LIMIT_MOVED, "src/writer.rs": self.LIMIT_FILES["src/writer.rs"]}
        self.assert_move_refused("two copies of const LIMIT", self.LIMIT_FILES, after)

    def test_an_item_move_is_refused_when_its_doc_comment_stays_behind(self) -> None:
        after = {**self.LIMIT_MOVED,
                 "src/constants.rs": self.LIMIT_MOVED["src/constants.rs"].replace("/// The most rows one write takes.\n", ""),
                 "src/writer.rs": self.LIMIT_MOVED["src/writer.rs"].replace(
                     "\npub(crate) fn", "\n/// The most rows one write takes.\npub(crate) fn")}
        self.assert_move_refused("the doc comment of const LIMIT", self.LIMIT_FILES, after)

    def test_an_item_move_is_refused_when_its_doc_comment_changes(self) -> None:
        after = {**self.LIMIT_MOVED,
                 "src/constants.rs": self.LIMIT_MOVED["src/constants.rs"].replace("most rows", "most lines")}
        self.assert_move_refused("the doc comment of const LIMIT", self.LIMIT_FILES, after)

    def test_an_item_move_is_refused_when_the_destination_already_names_it(self) -> None:
        collision = "pub(crate) fn rows(LIMIT: usize) -> usize { LIMIT }\n"
        before = {**self.LIMIT_FILES, "src/constants.rs": self.LIMIT_FILES["src/constants.rs"] + collision}
        after = {**self.LIMIT_MOVED, "src/constants.rs": before["src/constants.rs"]
                 + "\n/// The most rows one write takes.\npub(crate) const LIMIT: usize = 4;\n"}
        self.assert_move_refused("name collision: src/constants.rs already has LIMIT", before, after)

    def test_an_item_move_is_refused_when_a_repointed_path_is_inside_a_macro(self) -> None:
        before = {**self.LIMIT_FILES,
                  "src/reader.rs": "pub(crate) fn read() -> String { format!(\"{}\", crate::writer::LIMIT) }\n"}
        after = {**self.LIMIT_MOVED, "src/reader.rs": before["src/reader.rs"].replace("writer", "constants")}
        self.assert_move_refused("src/reader.rs: a path to LIMIT is inside a macro or an attribute", before, after)

    def test_a_struct_move_is_refused_when_the_source_reads_its_private_field(self) -> None:
        before = {
            "src/lib.rs": "mod model;\nmod writer;\n",
            "src/model.rs": "pub(crate) const OTHER: usize = 1;\n",
            "src/writer.rs": "pub(crate) struct Rows {\n    count: usize,\n}\n\n"
                             + "pub(crate) fn size(rows: &Rows) -> usize { rows.count }\n",
        }
        after = {
            "src/model.rs": before["src/model.rs"] + "\npub(crate) struct Rows {\n    count: usize,\n}\n",
            "src/writer.rs": "use crate::model::Rows;\n\npub(crate) fn size(rows: &Rows) -> usize { rows.count }\n",
        }
        self.assert_move_refused("the private member count of struct Rows", before, after)

    def test_a_function_move_is_refused_when_the_destination_has_another_trait_in_scope(self) -> None:
        label = "\npub(crate) fn label() -> String { \"rows\".to_string() }\n"
        before = {**self.LIMIT_FILES, "src/constants.rs": "use std::fmt::Write;\n\npub(crate) const OTHER: usize = 1;\n",
                  "src/writer.rs": self.LIMIT_FILES["src/writer.rs"] + label}
        after = {"src/constants.rs": before["src/constants.rs"] + label, "src/writer.rs": self.LIMIT_FILES["src/writer.rs"]}
        self.assert_move_refused("Write is in scope in only one of src/writer.rs and src/constants.rs", before, after)

    def test_an_item_move_is_refused_with_another_edit(self) -> None:
        after = {**self.LIMIT_MOVED, "src/reader.rs": self.LIMIT_MOVED["src/reader.rs"].replace("{ LIMIT }", "{ LIMIT + 0 }")}
        self.assert_move_refused("src/reader.rs: an edit is not a moved item", self.LIMIT_FILES, after)

    WIDE = "\npub(crate) fn wide() -> usize { max(1, 2) }\n"

    def test_a_function_move_is_refused_when_a_name_it_uses_binds_differently(self) -> None:
        before = {**self.LIMIT_FILES, "src/writer.rs": "use std::cmp::max;\n\n" + self.LIMIT_FILES["src/writer.rs"] + self.WIDE}
        after = {"src/writer.rs": self.LIMIT_FILES["src/writer.rs"],
                 "src/constants.rs": self.LIMIT_FILES["src/constants.rs"] + self.WIDE}
        self.assert_move_refused("max does not bind the same in src/writer.rs and in src/constants.rs", before, after)

    def test_a_function_move_reads_gen_as_a_name_before_edition_2024(self) -> None:
        # `gen` is a keyword only from 2024, so before it `value()` calls the `gen` of the file it is in.
        value = "pub(crate) fn value() -> usize { gen() }\n"
        before = {**self.LIMIT_FILES, "src/writer.rs": "fn gen() -> usize { 1 }\n\n" + value,
                  "src/constants.rs": "fn gen() -> usize { 2 }\n",
                  "src/reader.rs": "use crate::writer::value;\n\npub(crate) fn read() -> usize { value() }\n"}
        after = {"src/writer.rs": "pub(crate) use crate::constants::value;\n\nfn gen() -> usize { 1 }\n",
                 "src/constants.rs": "fn gen() -> usize { 2 }\n\n" + value}
        package = '[package]\nname = "sample"\nversion = "0.1.0"\n'
        self.assert_move_refused("gen does not bind the same in src/writer.rs and in src/constants.rs",
                                 {**before, "Cargo.toml": package + 'edition = "2021"\n'}, after)
        result = self.prove_move({**before, "Cargo.toml": package + 'edition = "2024"\n'}, after)
        assert isinstance(result, style_footprint.ItemMoves), result

    def test_an_item_move_drops_the_trailing_comma_of_a_gen_call_before_edition_2024(self) -> None:
        # Before 2024 `gen(LIMIT,)` is a call, whose trailing comma rustfmt drops. From 2024 `gen` is a keyword.
        write = "fn gen(a: usize) -> usize { a }\n\npub(crate) fn write() -> usize { gen(LIMIT,) }\n"
        before = {**self.LIMIT_FILES, "src/writer.rs": "/// The most rows one write takes.\n"
                  + "pub(crate) const LIMIT: usize = 4;\n\n" + write}
        after = {**self.LIMIT_MOVED, "src/writer.rs": "use crate::constants::LIMIT;\n\n" + write.replace(",)", ")")}
        package = '[package]\nname = "sample"\nversion = "0.1.0"\n'
        result = self.prove_move({**before, "Cargo.toml": package + 'edition = "2021"\n'}, after)
        assert isinstance(result, style_footprint.ItemMoves), result
        self.assert_move_refused("src/writer.rs: an edit is not a moved item",
                                 {**before, "Cargo.toml": package + 'edition = "2024"\n'}, after)

    def test_a_function_move_is_refused_when_its_added_use_can_shadow_a_name(self) -> None:
        pick = "pub(crate) fn pick(max: usize) -> usize { max }\n"
        before = {**self.LIMIT_FILES, "src/writer.rs": "use std::cmp::max;\n\n" + self.LIMIT_FILES["src/writer.rs"] + self.WIDE,
                  "src/constants.rs": pick}
        after = {"src/writer.rs": self.LIMIT_FILES["src/writer.rs"],
                 "src/constants.rs": "use std::cmp::max;\n\n" + pick + self.WIDE}
        self.assert_move_refused("src/constants.rs already names max", before, after)

    def test_an_item_move_is_refused_when_the_destination_gains_a_use_it_does_not_need(self) -> None:
        after = {**self.LIMIT_MOVED, "src/constants.rs": "use std::fmt;\n\n" + self.LIMIT_MOVED["src/constants.rs"]}
        self.assert_move_refused("src/constants.rs gains a use of fmt", self.LIMIT_FILES, after)

    def test_an_item_move_is_refused_when_the_new_module_line_is_more_visible(self) -> None:
        before = {**self.LIMIT_FILES, "src/lib.rs": "mod reader;\nmod writer;\n"}
        del before["src/constants.rs"]
        after = {**self.LIMIT_MOVED, "src/lib.rs": "pub mod constants;\nmod reader;\nmod writer;\n",
                 "src/constants.rs": "/// The most rows one write takes.\npub(crate) const LIMIT: usize = 4;\n"}
        self.assert_move_refused("`mod constants;` is more visible", before, after)

    def test_an_item_move_is_refused_when_the_new_module_holds_more(self) -> None:
        before = {**self.LIMIT_FILES, "src/lib.rs": "mod reader;\nmod writer;\n"}
        del before["src/constants.rs"]
        after = {**self.LIMIT_MOVED, "src/lib.rs": "mod constants;\nmod reader;\nmod writer;\n",
                 "src/constants.rs": "#![allow(dead_code)]\n\n/// The most rows one write takes.\n"
                                     + "pub(crate) const LIMIT: usize = 4;\n"}
        self.assert_move_refused("src/constants.rs: a new file holds more", before, after)

    def test_an_item_move_is_refused_when_the_source_still_names_it_without_a_use(self) -> None:
        after = {**self.LIMIT_MOVED, "src/writer.rs": "pub(crate) fn write() -> usize { LIMIT + 1 }\n"}
        self.assert_move_refused("src/writer.rs still names LIMIT, and no use brings it", self.LIMIT_FILES, after)

    def test_a_private_item_move_is_refused_when_the_source_still_uses_it(self) -> None:
        before = {**self.LIMIT_FILES, "src/writer.rs": self.LIMIT_FILES["src/writer.rs"].replace("pub(crate) const", "const"),
                  "src/reader.rs": "pub(crate) fn read() -> usize { 0 }\n"}
        after = {"src/writer.rs": self.LIMIT_MOVED["src/writer.rs"],
                 "src/constants.rs": self.LIMIT_MOVED["src/constants.rs"].replace("pub(crate) const LIMIT", "const LIMIT")}
        self.assert_move_refused("the private item LIMIT is still used outside src/constants.rs", before, after)

    def test_an_item_move_is_refused_when_the_item_has_a_super_path(self) -> None:
        other = "\npub(crate) fn other() -> usize { super::constants::OTHER }\n"
        before = {**self.LIMIT_FILES, "src/writer.rs": self.LIMIT_FILES["src/writer.rs"] + other}
        after = {"src/writer.rs": self.LIMIT_FILES["src/writer.rs"],
                 "src/constants.rs": self.LIMIT_FILES["src/constants.rs"] + other}
        self.assert_move_refused("fn other has a `super::` path", before, after)

    def test_an_item_move_is_refused_when_the_item_invokes_a_crate_macro(self) -> None:
        macro = "macro_rules! twice {\n    ($value:expr) => {\n        $value * 2\n    };\n}\n\n"
        before = {**self.LIMIT_FILES, "src/lib.rs": macro + self.LIMIT_FILES["src/lib.rs"],
                  "src/writer.rs": self.LIMIT_FILES["src/writer.rs"].replace("= 4;", "= twice!(2);")}
        after = {name: text.replace("= 4;", "= twice!(2);") for name, text in self.LIMIT_MOVED.items()}
        self.assert_move_refused("src/constants.rs: a macro call the proof cannot resolve: twice!", before, after)

    def test_an_item_move_is_refused_when_a_use_group_names_it(self) -> None:
        before = {**self.LIMIT_FILES, "src/reader.rs": "use crate::{constants::OTHER, writer::LIMIT};\n\n"
                                                       + "pub(crate) fn read() -> usize { LIMIT + OTHER }\n"}
        after = {**self.LIMIT_MOVED, "src/reader.rs": "use crate::constants::{LIMIT, OTHER};\n\n"
                                                      + "pub(crate) fn read() -> usize { LIMIT + OTHER }\n"}
        self.assert_move_refused("src/reader.rs: a use group names LIMIT", before, after)

    def test_an_item_move_is_refused_when_an_untouched_file_globs_the_source(self) -> None:
        before = {**self.LIMIT_FILES, "src/lib.rs": "mod constants;\nmod reader;\nmod report;\nmod writer;\n",
                  "src/report.rs": "use crate::writer::*;\n\npub(crate) fn report() -> usize { write() }\n"}
        self.assert_move_refused("src/report.rs: a glob use of crate::writer, a module the move changes", before,
                                 self.LIMIT_MOVED)
        for name, glob in self.HIDDEN_GLOBS:
            with self.subTest(name=name):
                report = glob.format(path="crate::writer") + "\npub(crate) fn report() -> usize { write() }\n"
                self.assert_move_refused("src/report.rs: a glob use of crate::writer, a module the move changes",
                                         {**before, "src/report.rs": report}, self.LIMIT_MOVED)

    # Glob uses with a `;` in a comment or a string before the `*`. A text search for `use`, then `*` before a `;`,
    # does not find the line comment forms. A block comment opens with `/*`, which that search reads as the glob.
    HIDDEN_GLOBS: tuple[tuple[str, str], ...] = (
        ("a line comment", "use {path}:: // compatibility;\n    *;\n"),
        ("a line comment in a use group", "use {path}::{{\n    write, // a; b\n    *,\n}};\n"),
        ("a block comment", "use {path}:: /* a; b */ *;\n"),
        ("a string before the use", 'const NOTE: &str = "use a; *";\n\nuse {path}:: // a;\n    *;\n'),
    )

    def test_a_function_move_is_refused_when_it_gains_sight_of_a_private_field(self) -> None:
        size = "\npub(crate) fn size(rows: &Rows) -> usize { rows.count }\n"
        rows = "pub(crate) struct Rows {\n    count: usize,\n}\n"
        before = {**self.LIMIT_FILES, "src/constants.rs": rows,
                  "src/writer.rs": "use crate::constants::Rows;\n\n" + self.LIMIT_FILES["src/writer.rs"] + size}
        after = {"src/writer.rs": self.LIMIT_FILES["src/writer.rs"], "src/constants.rs": rows + size}
        self.assert_move_refused("the private member count of struct Rows", before, after)

    def test_an_item_move_is_refused_when_the_new_module_has_a_module_doc(self) -> None:
        before = {**self.LIMIT_FILES, "src/lib.rs": "mod reader;\nmod writer;\n"}
        del before["src/constants.rs"]
        after = {**self.LIMIT_MOVED, "src/lib.rs": "mod constants;\nmod reader;\nmod writer;\n",
                 "src/constants.rs": "//! Shared limits.\n\n/// The most rows one write takes.\n"
                                     + "pub(crate) const LIMIT: usize = 4;\n"}
        self.assert_move_refused("src/constants.rs: a new file holds more", before, after)
        # A block comment before the module doc on its line does not hide it.
        for doc in ("/* Notes. */ //! Shared limits.\n", "/* Notes. */ /*! Shared limits. */\n"):
            with self.subTest(doc=doc):
                (self.repo / "src/constants.rs").unlink()
                constants = after["src/constants.rs"].replace("//! Shared limits.\n", doc)
                self.assert_move_refused("src/constants.rs: a new file holds more", before,
                                         {**after, "src/constants.rs": constants})

    def test_an_item_move_is_refused_when_a_touched_file_has_a_use_alias(self) -> None:
        after = {**self.LIMIT_MOVED, "src/reader.rs": "use crate::constants::LIMIT as MOST;\n\n"
                                                      + "pub(crate) fn read() -> usize { MOST }\n"}
        self.assert_move_refused("cannot read the use item", self.LIMIT_FILES, after)

    def test_an_item_move_is_refused_when_the_item_invokes_a_builtin_macro(self) -> None:
        origin = "\npub(crate) fn origin() -> &'static str {\n    module_path!()\n}\n"
        before = {**self.LIMIT_FILES, "src/writer.rs": self.LIMIT_FILES["src/writer.rs"] + origin}
        after = {"src/writer.rs": self.LIMIT_FILES["src/writer.rs"],
                 "src/constants.rs": self.LIMIT_FILES["src/constants.rs"] + origin}
        self.assert_move_refused("src/constants.rs: a macro call the proof cannot resolve: module_path!", before, after)

    def test_an_item_move_is_refused_when_the_item_has_a_cfg(self) -> None:
        before = {**self.LIMIT_FILES, "src/writer.rs": self.LIMIT_FILES["src/writer.rs"].replace(
            "pub(crate) const", "#[cfg(test)]\npub(crate) const")}
        after = {**self.LIMIT_MOVED, "src/constants.rs": self.LIMIT_MOVED["src/constants.rs"].replace(
            "pub(crate) const LIMIT", "#[cfg(test)]\npub(crate) const LIMIT")}
        self.assert_move_refused("const LIMIT has an attribute a macro can read: cfg", before, after)

    def test_an_item_move_is_refused_when_the_destination_mod_line_has_a_cfg(self) -> None:
        lib = "#[cfg(any())]\nmod constants;\nmod reader;\nmod writer;\n"
        before = {**self.LIMIT_FILES, "src/lib.rs": lib}
        after = {**self.LIMIT_MOVED, "src/lib.rs": lib}
        self.assert_move_refused("src/lib.rs: a cfg attribute on the path to crate::constants", before, after)

    def test_an_item_move_is_refused_when_the_destination_file_has_an_inner_cfg(self) -> None:
        before = {**self.LIMIT_FILES, "src/constants.rs": "#![cfg(any())]\n\n" + self.LIMIT_FILES["src/constants.rs"]}
        after = {**self.LIMIT_MOVED, "src/constants.rs": "#![cfg(any())]\n\n" + self.LIMIT_MOVED["src/constants.rs"]}
        self.assert_move_refused("src/constants.rs: a cfg attribute on the path to crate::constants", before, after)

    def test_an_item_move_is_refused_when_a_block_module_shadows_a_repointed_path(self) -> None:
        local = ("\npub(crate) fn local() -> usize {\n    mod writer {\n        pub(crate) const LIMIT: usize = 8;\n    }\n"
                 + "    mod constants {\n        pub(crate) const LIMIT: usize = 9;\n    }\n    writer::LIMIT\n}\n")
        before = {**self.LIMIT_FILES, "src/lib.rs": self.LIMIT_FILES["src/lib.rs"] + local}
        after = {**self.LIMIT_MOVED,
                 "src/lib.rs": before["src/lib.rs"].replace("    writer::LIMIT\n", "    constants::LIMIT\n")}
        self.assert_move_refused("src/lib.rs: constants is declared or bound outside a path", before, after)

    def test_an_item_move_is_refused_when_no_mod_line_declares_the_destination(self) -> None:
        before = {**self.LIMIT_FILES, "src/lib.rs": "mod reader;\nmod writer;\n"}
        self.assert_move_refused("no file declares `mod constants;` on the path to crate::constants", before,
                                 self.LIMIT_MOVED)

    def test_an_item_move_is_refused_when_a_raw_cfg_guards_the_destination(self) -> None:
        lib = "#[r#cfg(any())]\nmod constants;\nmod reader;\nmod writer;\n"
        self.assert_move_refused("src/lib.rs: a cfg attribute on the path to crate::constants",
                                 {**self.LIMIT_FILES, "src/lib.rs": lib}, {**self.LIMIT_MOVED, "src/lib.rs": lib})

    def test_an_item_move_is_refused_when_a_generic_parameter_shadows_a_repointed_path(self) -> None:
        pick = ("\npub(crate) trait HasLimit {\n    const LIMIT: usize;\n}\n\n"
                + "pub(crate) fn pick<writer: HasLimit, constants: HasLimit>() -> usize {\n    writer::LIMIT\n}\n")
        before = {**self.LIMIT_FILES, "src/lib.rs": self.LIMIT_FILES["src/lib.rs"] + pick}
        after = {**self.LIMIT_MOVED,
                 "src/lib.rs": before["src/lib.rs"].replace("    writer::LIMIT\n", "    constants::LIMIT\n")}
        self.assert_move_refused("src/lib.rs: constants is declared or bound outside a path", before, after)

    def test_an_item_move_is_refused_when_the_manifest_sets_a_library_path(self) -> None:
        manifest = '[package]\nname = "sample"\nversion = "0.1.0"\n\n[lib]\npath = "src/root.rs"\n'
        self.assert_move_refused("Cargo.toml sets a target path", {**self.LIMIT_FILES, "Cargo.toml": manifest},
                                 self.LIMIT_MOVED)

    def test_an_item_move_is_refused_when_the_manifest_cannot_be_read(self) -> None:
        self.assert_move_refused("cannot read Cargo.toml", {**self.LIMIT_FILES, "Cargo.toml": "[package\n"},
                                 self.LIMIT_MOVED)

    def test_an_item_move_is_refused_when_main_declares_a_module_beside_the_library(self) -> None:
        self.assert_move_refused("src/main.rs declares a module beside src/lib.rs",
                                 {**self.LIMIT_FILES, "src/main.rs": "mod writer;\n\nfn main() {}\n"}, self.LIMIT_MOVED)

    def test_an_item_move_is_refused_when_a_touched_use_has_a_cfg(self) -> None:
        before = {
            "src/lib.rs": "mod constants;\nmod custom;\nmod writer;\n",
            "src/custom.rs": "#[allow(non_upper_case_globals)]\npub(crate) const None: Option<u8> = Some(1);\n",
            "src/constants.rs": "use crate::custom::None;\n\npub(crate) const OTHER: Option<u8> = None;\n",
            "src/writer.rs": "#[cfg(any())]\nuse crate::custom::None;\n\npub(crate) const VALUE: Option<u8> = None;\n",
        }
        after = {"src/constants.rs": before["src/constants.rs"] + "\npub(crate) const VALUE: Option<u8> = None;\n",
                 "src/writer.rs": "#[cfg(any())]\nuse crate::custom::None;\n"}
        self.assert_move_refused("src/writer.rs: a use with an attribute", before, after)

    def test_an_item_move_is_refused_when_a_touched_file_has_a_raw_identifier(self) -> None:
        pick = ("\npub(crate) trait HasLimit {\n    const LIMIT: usize;\n}\n\n"
                + "pub(crate) fn pick<r#writer: HasLimit, r#constants: HasLimit>() -> usize {\n    writer::LIMIT\n}\n")
        before = {**self.LIMIT_FILES, "src/lib.rs": self.LIMIT_FILES["src/lib.rs"] + pick}
        after = {**self.LIMIT_MOVED,
                 "src/lib.rs": before["src/lib.rs"].replace("    writer::LIMIT\n", "    constants::LIMIT\n")}
        self.assert_move_refused("src/lib.rs: a raw identifier", before, after)

    def test_an_item_move_is_refused_when_a_block_extern_crate_binds_a_repointed_path(self) -> None:
        local = "\npub(crate) fn local() -> usize {\n    extern crate self as writer;\n    writer::LIMIT\n}\n"
        before = {**self.LIMIT_FILES, "src/lib.rs": self.LIMIT_FILES["src/lib.rs"] + local}
        after = {**self.LIMIT_MOVED,
                 "src/lib.rs": before["src/lib.rs"].replace("    writer::LIMIT\n", "    constants::LIMIT\n")}
        self.assert_move_refused("src/lib.rs: an `extern crate` that takes another name", before, after)

    def test_an_item_move_is_refused_when_the_item_reads_its_caller_location(self) -> None:
        line = "\npub(crate) fn line() -> u32 {\n    std::panic::Location::caller().line()\n}\n"
        before = {**self.LIMIT_FILES, "src/writer.rs": self.LIMIT_FILES["src/writer.rs"] + line}
        after = {"src/constants.rs": self.LIMIT_FILES["src/constants.rs"] + line,
                 "src/writer.rs": self.LIMIT_FILES["src/writer.rs"]}
        self.assert_move_refused("reads a caller location", before, after)

    SHAPE_FILES: dict[str, str] = {
        "src/lib.rs": "mod draw;\nmod shape;\n",
        "src/shape.rs": "pub(crate) fn unit() -> f64 { 1.0 }\n",
        "src/draw.rs": "/// A drawn point.\n#[derive(Debug, Clone)]\n#[must_use]\npub(crate) struct Point {\n"
                       + "    pub(crate) x: f64,\n}\n\npub(crate) fn draw() -> f64 { crate::shape::unit() }\n",
    }
    SHAPE_MOVED: dict[str, str] = {
        "src/shape.rs": "pub(crate) fn unit() -> f64 { 1.0 }\n\n/// A drawn point.\n#[derive(Debug, Clone)]\n#[must_use]\n"
                        + "pub(crate) struct Point {\n    pub(crate) x: f64,\n}\n",
        "src/draw.rs": "pub(crate) fn draw() -> f64 { crate::shape::unit() }\n",
    }

    def test_a_struct_with_standard_derives_and_must_use_moves_and_is_proven(self) -> None:
        result = self.prove_move(self.SHAPE_FILES, self.SHAPE_MOVED)
        assert isinstance(result, style_footprint.ItemMoves), result
        self.assertEqual([(move.kind, move.name) for move in result.moves], [("struct", "Point")])

    def test_an_item_move_is_refused_when_a_derive_can_read_the_module_path(self) -> None:
        before = {path: text.replace("Debug, Clone", "Debug, Reflect") for path, text in self.SHAPE_FILES.items()}
        after = {path: text.replace("Debug, Clone", "Debug, Reflect") for path, text in self.SHAPE_MOVED.items()}
        self.assert_move_refused("struct Point has an attribute a macro can read: Reflect", before, after)

    def test_an_item_move_is_refused_when_the_item_reads_a_type_name(self) -> None:
        name = "\npub(crate) fn name() -> &'static str {\n    std::any::type_name::<u8>()\n}\n"
        before = {**self.LIMIT_FILES, "src/writer.rs": self.LIMIT_FILES["src/writer.rs"] + name}
        after = {"src/constants.rs": self.LIMIT_FILES["src/constants.rs"] + name,
                 "src/writer.rs": self.LIMIT_FILES["src/writer.rs"]}
        self.assert_move_refused("reads a type name", before, after)

    def test_an_item_move_is_refused_when_a_commented_path_attribute_guards_the_destination(self) -> None:
        lib = '#[/* the platform file */ path = "platform.rs"]\nmod constants;\nmod reader;\nmod writer;\n'
        self.assert_move_refused("src/lib.rs: the attribute path on the path to crate::constants",
                                 {**self.LIMIT_FILES, "src/lib.rs": lib},
                                 {**self.LIMIT_MOVED, "src/lib.rs": lib})

    def test_an_item_move_is_refused_when_an_attribute_path_starts_with_an_inert_name(self) -> None:
        def context(files: dict[str, str]) -> dict[str, str]:
            return {path: text.replace("pub(crate) const LIMIT", "#[doc::context]\npub(crate) const LIMIT")
                    for path, text in files.items()}
        self.assert_move_refused("const LIMIT has an attribute a macro can read: doc::context",
                                 context(self.LIMIT_FILES), context(self.LIMIT_MOVED))

    def test_an_item_move_is_refused_when_a_derive_path_leaves_the_standard_library(self) -> None:
        before = {path: text.replace("Debug, Clone", "Debug, other::Clone") for path, text in self.SHAPE_FILES.items()}
        after = {path: text.replace("Debug, Clone", "Debug, other::Clone") for path, text in self.SHAPE_MOVED.items()}
        self.assert_move_refused("struct Point has an attribute a macro can read: other::Clone", before, after)

    def test_an_item_move_is_refused_when_an_associated_path_finds_another_trait(self) -> None:
        before = {
            "src/lib.rs": "mod constants;\nmod traits;\nmod writer;\n",
            "src/traits.rs": "pub(crate) trait Left {\n    const WIDTH: usize;\n}\n\npub(crate) trait Right {\n"
                             + "    const WIDTH: usize;\n}\n\nimpl Left for u8 {\n    const WIDTH: usize = 1;\n}\n\n"
                             + "impl Right for u8 {\n    const WIDTH: usize = 2;\n}\n",
            "src/constants.rs": "use crate::traits::Right;\n\npub(crate) const OTHER: usize = <u8 as Right>::WIDTH;\n",
            "src/writer.rs": "use crate::traits::Left;\n\npub(crate) const RESULT: usize = u8::WIDTH;\n\n"
                             + "pub(crate) const LEFT: usize = <u8 as Left>::WIDTH;\n",
        }
        after = {"src/constants.rs": before["src/constants.rs"] + "\npub(crate) const RESULT: usize = u8::WIDTH;\n",
                 "src/writer.rs": "use crate::traits::Left;\n\npub(crate) const LEFT: usize = <u8 as Left>::WIDTH;\n"}
        self.assert_move_refused("an associated path in const RESULT can change", before, after)

    def test_an_item_move_is_refused_when_an_untouched_file_reads_a_caller_location(self) -> None:
        here = "\npub(crate) fn here() -> u32 {\n    crate::position::line()\n}\n"
        position = "#[track_caller]\npub(crate) fn line() -> u32 {\n    std::panic::Location::caller().line()\n}\n"
        lib = "mod constants;\nmod position;\nmod reader;\nmod writer;\n"
        before = {**self.LIMIT_FILES, "src/lib.rs": lib, "src/position.rs": position,
                  "src/writer.rs": self.LIMIT_FILES["src/writer.rs"] + here}
        after = {"src/constants.rs": self.LIMIT_FILES["src/constants.rs"] + here,
                 "src/writer.rs": self.LIMIT_FILES["src/writer.rs"]}
        self.assert_move_refused("src/position.rs reads a caller location", before, after)

    def test_an_item_move_is_refused_when_a_commented_path_attribute_maps_another_module(self) -> None:
        lib = self.LIMIT_FILES["src/lib.rs"] + '#[/* the old file */ path = "writer.rs"]\nmod alias;\n'
        self.assert_move_refused("src/lib.rs: a `path` attribute", {**self.LIMIT_FILES, "src/lib.rs": lib},
                                 {**self.LIMIT_MOVED, "src/lib.rs": lib})

    def test_an_item_move_is_refused_when_an_attribute_macro_guards_the_destination(self) -> None:
        lib = "#[platform]\nmod constants;\nmod reader;\nmod writer;\n"
        self.assert_move_refused("src/lib.rs: the attribute platform on the path to crate::constants",
                                 {**self.LIMIT_FILES, "src/lib.rs": lib}, {**self.LIMIT_MOVED, "src/lib.rs": lib})

    def test_an_item_move_is_refused_when_an_attribute_on_the_destination_path_holds_a_comment(self) -> None:
        lib = "#[allow(/* the old note */ dead_code)]\nmod constants;\nmod reader;\nmod writer;\n"
        self.assert_move_refused("src/lib.rs: a comment in an attribute on the path to crate::constants",
                                 {**self.LIMIT_FILES, "src/lib.rs": lib}, {**self.LIMIT_MOVED, "src/lib.rs": lib})

    def test_an_item_move_is_refused_when_an_attribute_in_a_touched_file_holds_a_comment(self) -> None:
        def noted(files: dict[str, str]) -> dict[str, str]:
            return {path: text.replace("pub(crate) const LIMIT", "#[allow(/* note */ dead_code)]\npub(crate) const LIMIT")
                    for path, text in files.items()}
        self.assert_move_refused("src/writer.rs: a comment in an attribute", noted(self.LIMIT_FILES),
                                 noted(self.LIMIT_MOVED))

    def test_a_const_move_is_proven_beside_inert_and_test_only_mod_lines(self) -> None:
        lib = "#[allow(dead_code)]\nmod constants;\nmod reader;\n#[cfg(test)]\nmod tests;\n#[doc(hidden)]\nmod writer;\n"
        result = self.prove_move({**self.LIMIT_FILES, "src/lib.rs": lib}, {**self.LIMIT_MOVED, "src/lib.rs": lib})
        assert isinstance(result, style_footprint.ItemMoves), result
        self.assertEqual([(move.kind, move.name) for move in result.moves], [("const", "LIMIT")])

    def test_an_item_move_is_refused_when_a_macro_outside_a_fn_body_can_define_a_name(self) -> None:
        lib = ("macro_rules! make_value {\n    ($value:expr) => {\n        const fn value() -> usize { $value }\n"
               + "    };\n}\n\nmod constants;\nmod writer;\n")
        moved = "\npub(crate) fn read() -> usize { value() }\n"
        before = {"src/lib.rs": lib, "src/constants.rs": "make_value!(2);\n\npub(crate) const OTHER: usize = 1;\n",
                  "src/writer.rs": "make_value!(1);\n" + moved}
        after = {"src/constants.rs": before["src/constants.rs"] + moved, "src/writer.rs": "make_value!(1);\n"}
        self.assert_move_refused("src/constants.rs: a macro call the proof cannot resolve: make_value!", before, after)

    def test_an_item_move_is_refused_when_an_associated_path_finds_a_lowercase_trait(self) -> None:
        before = {
            "src/lib.rs": "mod constants;\nmod traits;\nmod writer;\n",
            "src/traits.rs": "pub(crate) trait left {\n    const WIDTH: usize;\n}\n\npub(crate) trait right {\n"
                             + "    const WIDTH: usize;\n}\n\nimpl left for u8 {\n    const WIDTH: usize = 1;\n}\n\n"
                             + "impl right for u8 {\n    const WIDTH: usize = 2;\n}\n",
            "src/constants.rs": "use crate::traits::right;\n\npub(crate) const OTHER: usize = <u8 as right>::WIDTH;\n",
            "src/writer.rs": "use crate::traits::left;\n\npub(crate) const RESULT: usize = u8::WIDTH;\n\n"
                             + "pub(crate) const LEFT: usize = <u8 as left>::WIDTH;\n",
        }
        after = {"src/constants.rs": before["src/constants.rs"] + "\npub(crate) const RESULT: usize = u8::WIDTH;\n",
                 "src/writer.rs": "use crate::traits::left;\n\npub(crate) const LEFT: usize = <u8 as left>::WIDTH;\n"}
        self.assert_move_refused("left is in scope in only one of src/writer.rs and src/constants.rs", before, after)

    def test_an_item_move_is_refused_when_an_untouched_file_reads_a_raw_caller_location(self) -> None:
        here = "\npub(crate) fn here() -> u32 {\n    crate::position::line()\n}\n"
        position = ("#[r#track_caller]\npub(crate) fn line() -> u32 {\n"
                    + "    std::panic::r#Location::r#caller().line()\n}\n")
        lib = "mod constants;\nmod position;\nmod reader;\nmod writer;\n"
        before = {**self.LIMIT_FILES, "src/lib.rs": lib, "src/position.rs": position,
                  "src/writer.rs": self.LIMIT_FILES["src/writer.rs"] + here}
        after = {"src/constants.rs": self.LIMIT_FILES["src/constants.rs"] + here,
                 "src/writer.rs": self.LIMIT_FILES["src/writer.rs"]}
        self.assert_move_refused("src/position.rs reads a caller location", before, after)

    def test_an_item_move_is_refused_when_a_use_names_a_custom_standard_derive(self) -> None:
        derive = "use custom_derives::Clone;\n\n"
        before = {**self.SHAPE_FILES, "src/shape.rs": derive + self.SHAPE_FILES["src/shape.rs"]}
        after = {**self.SHAPE_MOVED, "src/shape.rs": derive + self.SHAPE_MOVED["src/shape.rs"]}
        self.assert_move_refused("src/shape.rs imports Clone, the name of a standard derive", before, after)

    def test_an_item_move_is_refused_when_an_extern_crate_brings_its_macros(self) -> None:
        lib = "#[macro_use]\nextern crate custom_derives;\n\n" + self.SHAPE_FILES["src/lib.rs"]
        before = {**self.SHAPE_FILES, "src/lib.rs": lib}
        self.assert_move_refused("src/lib.rs: a `macro_use` can bring a macro named like a standard derive",
                                 before, self.SHAPE_MOVED)

    def test_an_item_move_is_refused_when_a_macro_use_list_names_a_standard_derive(self) -> None:
        lib = "#[macro_use(Clone)]\nextern crate custom_derives;\n\n" + self.SHAPE_FILES["src/lib.rs"]
        before = {**self.SHAPE_FILES, "src/lib.rs": lib}
        self.assert_move_refused("src/lib.rs: a `macro_use` can bring a macro named like a standard derive",
                                 before, self.SHAPE_MOVED)

    def test_a_struct_move_is_proven_beside_a_standard_debug_import_and_a_macro_use_module(self) -> None:
        derive = "use std::fmt::Debug;\n\n"
        lib = "mod draw;\n#[macro_use]\nmod macros;\nmod shape;\n"
        before = {**self.SHAPE_FILES, "src/lib.rs": lib, "src/macros.rs": "pub(crate) const UNUSED: u8 = 0;\n",
                  **{path: derive + self.SHAPE_FILES[path] for path in ("src/draw.rs", "src/shape.rs")}}
        after = {path: derive + text for path, text in self.SHAPE_MOVED.items()}
        result = self.prove_move(before, after)
        assert isinstance(result, style_footprint.ItemMoves), result

    def test_an_item_move_is_refused_when_the_destination_drops_the_prelude(self) -> None:
        inner = "#![no_implicit_prelude]\n\n"
        before = {**self.LIMIT_FILES, "src/constants.rs": inner + self.LIMIT_FILES["src/constants.rs"]}
        after = {**self.LIMIT_MOVED, "src/constants.rs": inner + self.LIMIT_MOVED["src/constants.rs"]}
        self.assert_move_refused("src/constants.rs: the inner attribute no_implicit_prelude on the path",
                                 before, after)

    def test_an_item_move_is_refused_when_an_attribute_macro_on_another_item_can_define_a_name(self) -> None:
        moved = "\npub(crate) fn read() -> usize { value() }\n"
        before = {"src/lib.rs": "mod constants;\nmod writer;\n",
                  "src/constants.rs": "#[codegen::make_value(2)]\nfn marker_b() {}\n",
                  "src/writer.rs": "#[codegen::make_value(1)]\nfn marker_a() {}\n" + moved}
        after = {"src/constants.rs": before["src/constants.rs"] + moved,
                 "src/writer.rs": "#[codegen::make_value(1)]\nfn marker_a() {}\n"}
        self.assert_move_refused("src/writer.rs has an attribute outside a fn body: codegen::make_value", before, after)

    def test_a_const_move_is_proven_beside_allowed_attributes_on_other_items(self) -> None:
        tests = ("\n#[cfg(test)]\nmod tests {\n    #[test]\n    fn reads() {}\n}\n\n#[derive(Debug)]\n#[must_use]\n"
                 + "pub(crate) struct Kept;\n")
        inner = "#![allow(dead_code)]\n//! The constants.\n\n"
        before = {**self.LIMIT_FILES, "src/writer.rs": self.LIMIT_FILES["src/writer.rs"] + tests,
                  "src/constants.rs": inner + self.LIMIT_FILES["src/constants.rs"]}
        after = {**self.LIMIT_MOVED, "src/writer.rs": self.LIMIT_MOVED["src/writer.rs"] + tests,
                 "src/constants.rs": inner + self.LIMIT_MOVED["src/constants.rs"]}
        result = self.prove_move(before, after)
        assert isinstance(result, style_footprint.ItemMoves), result

    def test_an_item_move_is_refused_when_an_extern_crate_takes_another_name(self) -> None:
        lib = "extern crate custom_derives as derives;\n\n" + self.SHAPE_FILES["src/lib.rs"]
        self.assert_move_refused("src/lib.rs: an `extern crate` that takes another name",
                                 {**self.SHAPE_FILES, "src/lib.rs": lib}, self.SHAPE_MOVED)

    def test_an_item_move_is_refused_when_a_dependency_takes_the_name_std(self) -> None:
        manifest = ('[package]\nname = "sample"\nversion = "0.1.0"\nedition = "2021"\n\n'
                    + "[target.'cfg(unix)'.dependencies]\n"
                    + 'std = { package = "custom_derives", path = "../custom_derives" }\n')
        derive = "use std::Clone;\n\n"
        before = {**self.SHAPE_FILES, "Cargo.toml": manifest,
                  **{path: derive + self.SHAPE_FILES[path] for path in ("src/draw.rs", "src/shape.rs")}}
        after = {path: derive + text for path, text in self.SHAPE_MOVED.items()}
        self.assert_move_refused("names std, and the crate gives the name to another crate or module", before, after)

    def test_an_item_move_is_refused_when_an_untouched_parent_drops_the_prelude(self) -> None:
        before = {"src/lib.rs": "mod beta;\nmod writer;\n",
                  "src/beta/mod.rs": "#![no_implicit_prelude]\n\nmod constants;\n",
                  "src/beta/constants.rs": self.LIMIT_FILES["src/constants.rs"],
                  "src/writer.rs": self.LIMIT_FILES["src/writer.rs"]}
        after = {"src/beta/constants.rs": self.LIMIT_MOVED["src/constants.rs"],
                 "src/writer.rs": self.LIMIT_MOVED["src/writer.rs"].replace("crate::constants",
                                                                            "crate::beta::constants")}
        self.assert_move_refused("src/beta/mod.rs: the inner attribute no_implicit_prelude on the path to crate::beta",
                                 before, after)

    def test_an_item_move_is_refused_when_the_source_drops_the_prelude_after_the_move(self) -> None:
        moved = "\npub(crate) fn read() -> usize { 1 }\n"
        before = {"src/lib.rs": "mod constants;\nmod writer;\n", "src/constants.rs": "fn marker_b() {}\n",
                  "src/writer.rs": "fn marker_a() {}\n" + moved}
        after = {"src/constants.rs": before["src/constants.rs"] + moved,
                 "src/writer.rs": "#![no_implicit_prelude]\n\nfn marker_a() {}\n"}
        self.assert_move_refused("src/writer.rs has an inner attribute outside a fn body: no_implicit_prelude",
                                 before, after)

    def test_an_item_move_is_refused_when_a_use_gives_the_name_std_to_a_module(self) -> None:
        lib = "mod draw;\nmod macros;\nmod shape;\n"
        rebind = "use crate::macros::std;\n\n"
        before = {**self.SHAPE_FILES, "src/lib.rs": lib, "src/macros.rs": "pub(crate) mod std {}\n",
                  **{path: rebind + self.SHAPE_FILES[path].replace("Debug, Clone", "Debug, std::Clone")
                     for path in ("src/draw.rs", "src/shape.rs")}}
        after = {path: rebind + text.replace("Debug, Clone", "Debug, std::Clone")
                 for path, text in self.SHAPE_MOVED.items()}
        self.assert_move_refused("src/draw.rs or src/shape.rs names std, and the crate gives the name", before, after)

    def test_a_struct_move_with_a_std_path_derive_is_proven_while_no_file_rebinds_std(self) -> None:
        before = {path: text.replace("Debug, Clone", "Debug, std::Clone") for path, text in self.SHAPE_FILES.items()}
        after = {path: text.replace("Debug, Clone", "Debug, std::Clone") for path, text in self.SHAPE_MOVED.items()}
        result = self.prove_move(before, after)
        assert isinstance(result, style_footprint.ItemMoves), result

    def test_an_item_move_is_refused_when_a_conditional_macro_use_brings_its_macros(self) -> None:
        for attribute in ("#[cfg_attr(all(), macro_use)]", "#[cfg_attr(all(), allow(unused), macro_use)]",
                          "#[cfg_attr(unix, cfg_attr(all(), macro_use))]", "#[cfg_attr(all(), macro_use(Clone))]",
                          "#[cfg_attr(all(), r#macro_use)]"):
            with self.subTest(attribute=attribute):
                lib = f"{attribute}\nextern crate custom_derives;\n\n" + self.SHAPE_FILES["src/lib.rs"]
                self.assert_move_refused("src/lib.rs: a `macro_use` can bring a macro named like a standard derive",
                                         {**self.SHAPE_FILES, "src/lib.rs": lib}, self.SHAPE_MOVED)

    def test_an_item_move_is_refused_when_a_rebound_std_hides_a_glob(self) -> None:
        before = {**self.LIMIT_FILES, "src/lib.rs": "mod constants;\nmod reader;\nmod report;\nmod writer;\n",
                  "src/report.rs": "use crate::writer as std;\nuse std::*;\n\n"
                                   + "pub(crate) fn report() -> usize { write() }\n"}
        self.assert_move_refused("src/report.rs: a glob use the proof cannot resolve", before, self.LIMIT_MOVED)

    def test_an_item_move_is_refused_when_a_or_b_globs_another_crate(self) -> None:
        manifest = ('[package]\nname = "sample"\nversion = "0.1.0"\nedition = "2021"\n\n'
                    + '[dependencies]\ncustom_derives = { path = "../custom_derives" }\n')
        glob = "use custom_derives::*;\n\n"
        files = ("src/draw.rs", "src/shape.rs")
        before = {**self.SHAPE_FILES, "Cargo.toml": manifest,
                  **{path: glob + self.SHAPE_FILES[path] for path in files}}
        after = {path: glob + text for path, text in self.SHAPE_MOVED.items()}
        self.assert_move_refused("src/draw.rs: a glob use of the crate custom_derives", before, after)

    def test_an_item_move_is_refused_when_a_macro_binds_a_glob_head(self) -> None:
        lib = ("macro_rules! bind_bridge {\n    () => { use crate::writer as bridge; };\n}\n\n"
               + "mod constants;\nmod reader;\nmod report;\nmod writer;\n")
        before = {**self.LIMIT_FILES, "src/lib.rs": lib,
                  "src/report.rs": "bind_bridge!();\nuse bridge::*;\n\npub(crate) fn report() -> usize { write() }\n"}
        self.assert_move_refused("src/lib.rs: a crate macro the proof cannot read", before, self.LIMIT_MOVED)

    def test_an_item_move_is_refused_when_a_macro_binds_its_arguments(self) -> None:
        lib = ("macro_rules! module_alias {\n    ($alias:ident :: $source:ident) => {\n"
               + "        use crate::$source as $alias;\n    };\n}\n\n"
               + "mod constants;\nmod reader;\nmod report;\nmod writer;\n")
        report = ("module_alias!(alloc::writer);\nuse alloc::*;\n\n"
                  + "pub(crate) fn report(rows: usize) -> bool { matches!(rows, LIMIT) }\n")
        before = {**self.LIMIT_FILES, "src/lib.rs": lib, "src/report.rs": report}
        self.assert_move_refused("src/lib.rs: a crate macro the proof cannot read", before, self.LIMIT_MOVED)

    def test_an_item_move_is_refused_when_a_macro_takes_a_keyword_as_an_argument(self) -> None:
        lib = ("macro_rules! module_alias {\n    ($u:tt $alias:ident :: $source:ident $a:tt) => {\n"
               + "        $u crate::$source $a $alias;\n    };\n}\n\n"
               + "mod constants;\nmod reader;\nmod report;\nmod writer;\n")
        report = ("module_alias!(use alloc::writer as);\nuse alloc::*;\n\n"
                  + "pub(crate) fn report(rows: usize) -> bool { matches!(rows, LIMIT) }\n")
        before = {**self.LIMIT_FILES, "src/lib.rs": lib, "src/report.rs": report}
        self.assert_move_refused("src/lib.rs: a crate macro the proof cannot read", before, self.LIMIT_MOVED)

    def test_an_item_move_is_refused_when_a_crate_macro_can_make_a_binding(self) -> None:
        cases = (
            ("a glob", "macro_rules! globs {\n    () => { use crate::writer::*; };\n}\n\n"),
            ("an extern crate", "macro_rules! bring {\n    () => { extern crate alloc; };\n}\n\n"),
            ("a module", "macro_rules! module {\n    () => { mod helpers {} };\n}\n\n"),
            ("a cast", "macro_rules! narrow {\n    ($value:expr) => { $value as u8 };\n}\n\n"),
            ("a raw use", "macro_rules! raw {\n    () => { r#use };\n}\n\n"),
            ("a macro item", "pub(crate) macro bridge($value:expr) {\n    $value\n}\n\n"),
            ("a nested macro_rules", "macro_rules! outer {\n    () => { macro_rules! inner { () => { 1 }; } };\n}\n\n"),
            *((f"a {fragment} fragment", f"macro_rules! carry {{\n    ($value:{fragment}) => {{ $value }};\n}}\n\n")
              for fragment in ("tt", "ident", "item", "stmt", "meta", "vis")),
        )
        for name, macro in cases:
            with self.subTest(name=name):
                before = {**self.LIMIT_FILES, "src/lib.rs": macro + self.LIMIT_FILES["src/lib.rs"]}
                self.assert_move_refused("src/lib.rs: a crate macro the proof cannot read", before,
                                         self.LIMIT_MOVED)
        with self.subTest(name="added after the change"):
            after = {**self.LIMIT_MOVED, "src/writer.rs": cases[0][1] + self.LIMIT_MOVED["src/writer.rs"]}
            self.assert_move_refused("src/writer.rs: a crate macro the proof cannot read", self.LIMIT_FILES,
                                     after)

    def test_an_item_move_is_refused_when_a_crate_file_includes_code(self) -> None:
        cases = (
            ("src/lib.rs", "fn bindings() {\n    include!(\"bindings.inc\");\n}\n\n"),
            ("src/lib.rs", "include!(\"bindings.inc\");\n\n"),
            ("src/lib.rs", "r#include!(\"bindings.inc\");\n\n"),
            ("src/lib.rs", "std::include!(\"bindings.inc\");\n\n"),
        )
        for path, code in cases:
            with self.subTest(code=code):
                before = {**self.LIMIT_FILES, "src/bindings.inc": "use crate::writer as alloc;\n",
                          path: code + self.LIMIT_FILES[path]}
                self.assert_move_refused(f"{path}: an include! brings code the proof cannot read", before,
                                         self.LIMIT_MOVED)

    def test_an_item_move_is_refused_when_a_crate_file_names_include(self) -> None:
        cases = (
            "use std::include as splice;\n\n"
            + "macro_rules! module_alias {\n    () => { crate::splice!(\"bindings.inc\"); };\n}\n\n",
            "use core::include;\n\n",
            "use std::{include as x};\n\n",
        )
        for code in cases:
            with self.subTest(code=code):
                before = {**self.LIMIT_FILES, "src/bindings.inc": "use crate::writer as alloc;\n",
                          "src/lib.rs": code + self.LIMIT_FILES["src/lib.rs"]}
                self.assert_move_refused("src/lib.rs: a use or path of include! brings code the proof cannot read",
                                         before, self.LIMIT_MOVED)

    def test_an_item_move_is_refused_beside_any_crate_macro(self) -> None:
        macro = ("macro_rules! twice {\n    ($e:expr) => { $e * 2 };\n}\n\n"
                 + "fn four() -> usize {\n    twice!(2)\n}\n\n")
        before = {**self.LIMIT_FILES, "src/lib.rs": macro + self.LIMIT_FILES["src/lib.rs"]}
        self.assert_move_refused("src/lib.rs: a crate macro the proof cannot read", before, self.LIMIT_MOVED)

    def test_an_item_move_is_refused_beside_a_macro_call_it_does_not_list(self) -> None:
        cases = (
            ("an unknown macro", "codegen::make_items!(alloc);\n\n", "codegen::make_items"),
            ("a const initializer", "const NOTES: &str = include_str!(\"notes.txt\");\n\n", "include_str"),
            ("a listed name from another crate", "const NAME: &str = tracing::concat!(\"a\");\n\n", "concat"),
            ("an impl body", "struct Probe;\n\nimpl Probe {\n    paste::item!();\n}\n\n", "paste::item"),
            ("a fn body whose call can emit a glob use",
             "fn wide(rows: usize) -> bool {\n    provider::writer_imports!();\n"
             + "    match rows {\n        LIMIT => true,\n        _ => false,\n    }\n}\n\n", "writer_imports"),
            ("include_bytes in a fn body", "fn notes() -> &'static [u8] {\n    include_bytes!(\"notes.txt\")\n}\n\n",
             "include_bytes"),
            ("a raw keyword name", "fn wait() {\n    provider::r#async!();\n}\n\n", "async"),
        )
        for name, code, call in cases:
            with self.subTest(name=name):
                before = {**self.LIMIT_FILES, "src/notes.txt": "use crate::writer as alloc;\n",
                          "src/lib.rs": code + self.LIMIT_FILES["src/lib.rs"]}
                self.assert_move_refused(f"src/lib.rs: a macro call the proof cannot resolve: {call.split('::')[-1]}!",
                                         before, self.LIMIT_MOVED)

    def test_an_item_move_is_refused_beside_a_use_of_a_macro_name(self) -> None:
        cases = (
            ("an alias", "use tracing::info as log;\n\n"),
            ("a use leaf", "use tracing::info;\n\n"),
            ("a use group", "use tracing::{info, Level};\n\n"),
            ("a macro_use list", "#[macro_use(info)]\nextern crate tracing;\n\n"),
        )
        call = "fn trace() {\n    tracing::info!(\"moved\");\n}\n\n"
        for name, code in cases:
            with self.subTest(name=name):
                before = {**self.LIMIT_FILES, "src/lib.rs": code + call + self.LIMIT_FILES["src/lib.rs"]}
                self.assert_move_refused("src/lib.rs: a use or path of info! the proof cannot resolve", before,
                                         self.LIMIT_MOVED)
        with self.subTest(name="a raw keyword name"):
            code = "use provider::r#async as wait;\n\nfn hold() {\n    r#async!();\n}\n\n"
            before = {**self.LIMIT_FILES, "src/lib.rs": code + self.LIMIT_FILES["src/lib.rs"]}
            self.assert_move_refused("src/lib.rs: a use or path of async! the proof cannot resolve", before,
                                     self.LIMIT_MOVED)

    def test_an_item_move_is_refused_beside_a_std_macro_path_the_crate_gives_away(self) -> None:
        package = '[package]\nname = "sample"\nversion = "0.1.0"\nedition = "2021"\n\n'
        cases = (
            ("a dependency key std", package + '[dependencies]\nstd = { package = "provider", version = "1" }\n',
             "", "std::concat"),
            ("a workspace dependency key core", package + "[build-dependencies]\ncore = { workspace = true }\n",
             "", "::core::concat"),
            ("a target dependency key alloc",
             package + "[target.'cfg(unix)'.dev-dependencies]\nalloc = { package = \"provider\", version = \"1\" }\n",
             "", "std::concat"),
            ("a file that binds core", package, "use crate::writer as core;\n\n", "core::concat"),
        )
        for name, manifest, alias, call in cases:
            with self.subTest(name=name):
                code = f"{alias}const NAME: &str = {call}!(\"a\");\n\n"
                before = {**self.LIMIT_FILES, "Cargo.toml": manifest,
                          "src/lib.rs": code + self.LIMIT_FILES["src/lib.rs"]}
                self.assert_move_refused("src/lib.rs: a macro call the proof cannot resolve: concat!", before,
                                         self.LIMIT_MOVED)

    def test_an_item_move_is_refused_beside_a_macro_named_like_a_keyword_of_a_later_edition(self) -> None:
        package = '[package]\nname = "sample"\nversion = "0.1.0"\n'
        workspace = '[workspace]\n\n[workspace.package]\nedition = "2018"\n\n' + package + "edition.workspace = true\n"
        cases = (
            ("gen in edition 2021", package + 'edition = "2021"\n', "gen"),
            ("gen in a workspace edition 2018", workspace, "gen"),
            ("the weak keyword union", package + 'edition = "2024"\n', "union"),
            ("the weak keyword safe", package + 'edition = "2024"\n', "safe"),
        )
        for name, manifest, call in cases:
            with self.subTest(name=name):
                code = f"fn make() {{\n    provider::{call}!();\n}}\n\n"
                before = {**self.LIMIT_FILES, "Cargo.toml": manifest,
                          "src/lib.rs": code + self.LIMIT_FILES["src/lib.rs"]}
                self.assert_move_refused(f"src/lib.rs: a macro call the proof cannot resolve: {call}!", before,
                                         self.LIMIT_MOVED)

    def test_an_item_move_is_refused_beside_a_bare_std_macro_another_crate_can_give(self) -> None:
        cases = (
            ("a glob of another crate", "use provider::*;\n\n"),
            ("a glob in a use group", "use provider::{fmt::*, Error};\n\n"),
            ("a glob in a fn body", "fn wide() {\n    use ::provider::*;\n}\n\n"),
            ("a glob of a crate name a file binds", "use crate::writer as alloc;\n\nuse alloc::*;\n\n"),
            ("a macro_use", "#[macro_use]\nextern crate provider;\n\n"),
            ("a macro_use in a cfg_attr", "#[cfg_attr(unix, cfg_attr(test, macro_use))]\nmod helpers {}\n\n"),
            ("an inner macro_use", "#![macro_use]\n\n"),
        )
        for name, code in cases:
            with self.subTest(name=name):
                before = {**self.LIMIT_FILES, "src/lib.rs": code + "const NAME: &str = concat!(\"a\");\n\n"
                          + self.LIMIT_FILES["src/lib.rs"]}
                self.assert_move_refused("src/lib.rs: a macro call the proof cannot resolve: concat!", before,
                                         self.LIMIT_MOVED)
        for name, glob in self.HIDDEN_GLOBS:
            with self.subTest(name=f"a glob after {name}"):
                before = {**self.LIMIT_FILES, "src/lib.rs": glob.format(path="provider")
                          + "\nconst NAME: &str = concat!(\"a\");\n\n" + self.LIMIT_FILES["src/lib.rs"]}
                self.assert_move_refused("src/lib.rs: a macro call the proof cannot resolve: concat!", before,
                                         self.LIMIT_MOVED)
        with self.subTest(name="a glob the change adds"):
            after = {**self.LIMIT_MOVED, "src/reader.rs": "use provider::*;\n\n" + self.LIMIT_MOVED["src/reader.rs"]}
            before = {**self.LIMIT_FILES, "src/lib.rs": "const NAME: &str = concat!(\"a\");\n\n"
                      + self.LIMIT_FILES["src/lib.rs"]}
            self.assert_move_refused("src/lib.rs: a macro call the proof cannot resolve: concat!", before, after)
        with self.subTest(name="a glob of a crate path to another crate"):
            code = "mod bridge {\n    pub(crate) use provider as found;\n}\n\nuse crate::bridge::found::*;\n\n"
            before = {**self.LIMIT_FILES, "src/lib.rs": code + "const NAME: &str = concat!(\"a\");\n\n"
                      + self.LIMIT_FILES["src/lib.rs"]}
            self.assert_move_refused("src/lib.rs: a glob use of crate::bridge::found, which has no file of its own",
                                     before, self.LIMIT_MOVED)

    def test_an_item_move_is_proven_beside_macro_calls_it_accepts(self) -> None:
        code = ("use std::fmt::*;\n\nconst NAME: &str = concat!(\"a\", \"b\");\n\n"
                + "const PATH: &str = ::std::stringify!(a);\n\n"
                + "fn trace() -> String {\n    println!(\"moved\");\n    core::format!(\"{}\", NAME)\n}\n\n"
                + "impl Probe {\n    fn check(&self) {\n        assert!(matches!(1, 1));\n    }\n}\n\n")
        result = self.prove_move({**self.LIMIT_FILES, "src/lib.rs": code + self.LIMIT_FILES["src/lib.rs"]},
                                 self.LIMIT_MOVED)
        assert isinstance(result, style_footprint.ItemMoves), result
        self.assertEqual([(move.kind, move.name) for move in result.moves], [("const", "LIMIT")])
        # A glob in a comment or a string opens no name.
        code = '// use provider::*;\nconst NOTE: &str = "use provider::*;";\n\nconst NAME: &str = concat!("a");\n\n'
        result = self.prove_move({**self.LIMIT_FILES, "src/lib.rs": code + self.LIMIT_FILES["src/lib.rs"]},
                                 self.LIMIT_MOVED)
        assert isinstance(result, style_footprint.ItemMoves), result

    def test_an_item_move_is_refused_in_a_crate_older_than_rust_2018(self) -> None:
        package = '[package]\nname = "sample"\nversion = "0.1.0"\n'
        workspace = '[workspace]\n\n[workspace.package]\nedition = "2021"\n\n' + package + "edition.workspace = true\n"
        for manifest, reason in ((package, "the crate's edition is 2015"),
                                 (package + 'edition = "2015"\n', "the crate's edition is 2015"),
                                 (workspace.replace('edition = "2021"\n', ""), "the crate's edition is unknown")):
            with self.subTest(manifest=manifest):
                self.assert_move_refused(reason, {**self.LIMIT_FILES, "Cargo.toml": manifest}, self.LIMIT_MOVED)
        result = self.prove_move({**self.LIMIT_FILES, "Cargo.toml": workspace}, self.LIMIT_MOVED)
        assert isinstance(result, style_footprint.ItemMoves), result

    # Two consts leave src/writer.rs: LIMIT for the existing src/constants.rs, WIDTH for the new src/sizes.rs.
    SPLIT_FILES: dict[str, str] = {
        "src/lib.rs": "mod constants;\nmod reader;\nmod writer;\n",
        "src/constants.rs": "pub(crate) const OTHER: usize = 1;\n",
        "src/writer.rs": "/// The most rows one write takes.\npub(crate) const LIMIT: usize = 4;\n\n"
                         + "/// The widest row.\npub(crate) const WIDTH: usize = 8;\n\n"
                         + "pub(crate) fn write() -> usize { LIMIT + WIDTH }\n",
        "src/reader.rs": "use crate::writer::LIMIT;\nuse crate::writer::WIDTH;\n\n"
                         + "pub(crate) fn read() -> usize { LIMIT + WIDTH }\n",
    }
    SPLIT_MOVED: dict[str, str] = {
        "src/lib.rs": "mod constants;\nmod reader;\nmod sizes;\nmod writer;\n",
        "src/constants.rs": "pub(crate) const OTHER: usize = 1;\n\n/// The most rows one write takes.\n"
                            + "pub(crate) const LIMIT: usize = 4;\n",
        "src/sizes.rs": "/// The widest row.\npub(crate) const WIDTH: usize = 8;\n",
        "src/writer.rs": "use crate::constants::LIMIT;\nuse crate::sizes::WIDTH;\n\n"
                         + "pub(crate) fn write() -> usize { LIMIT + WIDTH }\n",
        "src/reader.rs": "use crate::constants::LIMIT;\nuse crate::sizes::WIDTH;\n\n"
                         + "pub(crate) fn read() -> usize { LIMIT + WIDTH }\n",
    }

    def test_two_items_moved_from_one_file_to_an_existing_and_a_new_module_are_proven(self) -> None:
        result = self.prove_move(self.SPLIT_FILES, self.SPLIT_MOVED)
        assert isinstance(result, style_footprint.ItemMoves), result
        self.assertEqual({(move.name, move.destination.path) for move in result.moves},
                         {("LIMIT", "src/constants.rs"), ("WIDTH", "src/sizes.rs")})
        [line] = result.module_lines
        self.assertEqual(self.SPLIT_MOVED["src/lib.rs"][line.start:line.end], "mod sizes;")
        self.assertEqual(len([span for span in result.spans("src/writer.rs", False)
                              if span in {move.source for move in result.moves}]), 2)
        self.assertEqual({site.text for site in result.sites}, {"crate::constants", "crate::sizes"})

    # Two pairs with different sources: WIDTH leaves src/reader.rs for src/sizes.rs, and LIMIT leaves src/writer.rs
    # for src/constants.rs. The writer pair comes second.
    PAIRS_FILES: dict[str, str] = {
        "src/lib.rs": "mod caller;\nmod constants;\nmod reader;\nmod sizes;\nmod writer;\n",
        "src/caller.rs": "pub(crate) fn call() -> usize { 3 }\n",
        "src/constants.rs": "pub(crate) const OTHER: usize = 1;\n",
        "src/sizes.rs": "pub(crate) const HEIGHT: usize = 2;\n",
        "src/writer.rs": "/// The most rows one write takes.\npub(crate) const LIMIT: usize = 4;\n\n"
                         + "pub(crate) fn write() -> usize { LIMIT + 1 }\n",
        "src/reader.rs": "use crate::writer::LIMIT;\n\n/// The widest row.\npub(crate) const WIDTH: usize = 8;\n\n"
                         + "pub(crate) fn read() -> usize { LIMIT + WIDTH }\n",
    }
    PAIRS_MOVED: dict[str, str] = {
        "src/constants.rs": "pub(crate) const OTHER: usize = 1;\n\n/// The most rows one write takes.\n"
                            + "pub(crate) const LIMIT: usize = 4;\n",
        "src/sizes.rs": "pub(crate) const HEIGHT: usize = 2;\n\n/// The widest row.\n"
                        + "pub(crate) const WIDTH: usize = 8;\n",
        "src/writer.rs": "use crate::constants::LIMIT;\n\npub(crate) fn write() -> usize { LIMIT + 1 }\n",
        "src/reader.rs": "use crate::constants::LIMIT;\nuse crate::sizes::WIDTH;\n\n"
                         + "pub(crate) fn read() -> usize { LIMIT + WIDTH }\n",
    }

    def test_two_pairs_with_different_sources_are_proven(self) -> None:
        result = self.prove_move(self.PAIRS_FILES, self.PAIRS_MOVED)
        assert isinstance(result, style_footprint.ItemMoves), result
        self.assertEqual({(move.source.path, move.name, move.destination.path) for move in result.moves},
                         {("src/writer.rs", "LIMIT", "src/constants.rs"), ("src/reader.rs", "WIDTH", "src/sizes.rs")})
        self.assertEqual({(site.path, site.text) for site in result.sites}, {("src/reader.rs", "crate::constants")})

    def test_an_item_move_is_refused_when_a_file_is_a_source_and_a_destination(self) -> None:
        before = {**self.LIMIT_FILES, "src/constants.rs": "/// One more.\npub(crate) const OTHER: usize = 1;\n"}
        after = {**self.LIMIT_MOVED, "src/constants.rs": "/// The most rows one write takes.\n"
                                                         + "pub(crate) const LIMIT: usize = 4;\n",
                 "src/writer.rs": "use crate::constants::LIMIT;\n\n/// One more.\n"
                                  + "pub(crate) const OTHER: usize = 1;\n\n"
                                  + "pub(crate) fn write() -> usize { LIMIT + 1 }\n"}
        self.assert_move_refused("src/constants.rs is both a source and a destination of the move", before, after)

    def test_an_item_move_is_refused_when_one_item_moves_to_two_files(self) -> None:
        after = {**self.SPLIT_MOVED, "src/sizes.rs": self.SPLIT_MOVED["src/sizes.rs"] + "\n/// The most rows one write "
                                                     + "takes.\npub(crate) const LIMIT: usize = 4;\n"}
        self.assert_move_refused("two copies of const LIMIT", self.SPLIT_FILES, after)

    def test_an_item_move_is_refused_when_the_second_pair_changes_a_literal(self) -> None:
        after = {**self.PAIRS_MOVED, "src/constants.rs": self.PAIRS_MOVED["src/constants.rs"].replace("= 4", "= 5")}
        self.assert_move_refused("LIMIT changes during the move", self.PAIRS_FILES, after)

    def test_an_item_move_is_refused_when_the_second_pair_misses_a_reexport(self) -> None:
        caller = "pub(crate) fn call() -> usize { crate::writer::LIMIT }\n"
        self.assert_move_refused("LIMIT leaves src/writer.rs without a re-export, and src/caller.rs still uses its old "
                                 + "path", {**self.PAIRS_FILES, "src/caller.rs": caller}, self.PAIRS_MOVED)

    def test_an_item_move_is_refused_when_a_use_in_b_names_another_pairs_item_by_its_old_path(self) -> None:
        # LIMIT reads WIDTH, which leaves src/reader.rs with no re-export, so `crate::reader::WIDTH` names nothing.
        before = {**self.PAIRS_FILES, "src/writer.rs": "use crate::reader::WIDTH;\n\n"
                                                       + self.PAIRS_FILES["src/writer.rs"].replace("= 4", "= WIDTH")}
        after = {**self.PAIRS_MOVED,
                 "src/constants.rs": "use crate::reader::WIDTH;\n\n"
                                     + self.PAIRS_MOVED["src/constants.rs"].replace("= 4", "= WIDTH"),
                 "src/writer.rs": self.PAIRS_MOVED["src/writer.rs"]}
        self.assert_move_refused("WIDTH does not bind the same in src/writer.rs and in src/constants.rs", before, after)
        reexport = self.PAIRS_MOVED["src/reader.rs"].replace("use crate::sizes", "pub(crate) use crate::sizes")
        result = self.prove_move(before, {**after, "src/reader.rs": reexport})
        assert isinstance(result, style_footprint.ItemMoves), result

    def test_an_item_move_is_refused_when_a_moved_item_names_a_moved_item_by_a_path(self) -> None:
        before = {"src/lib.rs": "mod constants;\nmod outer;\n",
                  "src/constants.rs": "pub(crate) const OTHER: usize = 1;\n",
                  "src/outer/mod.rs": "pub(crate) mod writer;\n",
                  "src/outer/writer.rs": "use crate::outer;\n\npub(crate) const BASE: usize = 2;\n\n"
                                         + "pub(crate) const LIMIT: usize = outer::writer::BASE * 2;\n\n"
                                         + "pub(crate) fn write() -> usize { 1 }\n"}
        after = {"src/constants.rs": "use crate::outer;\n\npub(crate) const OTHER: usize = 1;\n\n"
                                     + "pub(crate) const BASE: usize = 2;\n\n"
                                     + "pub(crate) const LIMIT: usize = outer::writer::BASE * 2;\n",
                 "src/outer/writer.rs": "pub(crate) fn write() -> usize { 1 }\n"}
        self.assert_move_refused("LIMIT names the moved item BASE by a path", before, after)

    # src/reader.rs, which the move touches, globs src/helpers.rs, which it does not.
    GLOB_FILES: dict[str, str] = {
        **LIMIT_FILES, "src/lib.rs": "mod constants;\nmod helpers;\nmod reader;\nmod writer;\n",
        "src/helpers.rs": "pub(crate) fn help() -> usize { 2 }\n",
        "src/reader.rs": "use crate::helpers::*;\nuse crate::writer::LIMIT;\n\n"
                         + "pub(crate) fn read() -> usize { LIMIT + help() }\n",
    }
    GLOB_MOVED: dict[str, str] = {
        **LIMIT_MOVED, "src/reader.rs": "use crate::helpers::*;\nuse crate::constants::LIMIT;\n\n"
                                        + "pub(crate) fn read() -> usize { LIMIT + help() }\n",
    }

    def test_a_move_is_proven_beside_a_glob_of_a_module_that_exports_only_its_own_items(self) -> None:
        result = self.prove_move(self.GLOB_FILES, self.GLOB_MOVED)
        assert isinstance(result, style_footprint.ItemMoves), result

    def test_a_move_is_refused_beside_a_glob_that_can_import_from_a_or_b(self) -> None:
        helpers = self.GLOB_FILES["src/helpers.rs"]
        tests = "\n#[cfg(test)]\nmod tests {\n    use super::*;\n}\n"
        reader, moved = self.GLOB_FILES["src/reader.rs"], self.GLOB_MOVED["src/reader.rs"]
        cases: tuple[tuple[str, dict[str, str], dict[str, str]], ...] = (
            ("a glob use of crate::helpers, which holds a glob use or a public use",
             {"src/helpers.rs": "pub(crate) use crate::writer::write;\n\n" + helpers}, {}),
            ("a glob use of crate::helpers, which holds a glob use or a public use",
             {"src/helpers.rs": "use std::fmt::*;\n\n" + helpers}, {}),
            ("a glob use of crate::helpers, which holds a restricted visibility",
             {"src/helpers.rs": helpers.replace("pub(crate)", "pub(super)")}, {}),
            ("a glob use of crate::helpers, which has a macro outside a fn body: stringify",
             {"src/helpers.rs": "const ROWS: &str = stringify!(rows);\n\n" + helpers}, {}),
            ("a glob use of crate::helpers, which the change touches",
             {"src/helpers.rs": "pub(crate) fn help() -> usize { crate::writer::LIMIT }\n"},
             {"src/helpers.rs": "pub(crate) fn help() -> usize { crate::constants::LIMIT }\n"}),
            ("a glob use of crate::helpers::inner, which has no file of its own",
             {"src/helpers.rs": "pub(crate) mod inner {\n    pub(crate) fn help() -> usize { 2 }\n}\n",
              "src/reader.rs": reader.replace("helpers::*", "helpers::inner::*")},
             {"src/reader.rs": moved.replace("helpers::*", "helpers::inner::*")}),
            ("src/reader.rs: a glob use of crate, a module the move changes or a parent of one",
             {"src/reader.rs": reader.replace("helpers::*", "*")}, {"src/reader.rs": moved.replace("helpers::*", "*")}),
            ("src/writer.rs: a glob use of crate::writer, a module the move changes",
             {"src/writer.rs": self.LIMIT_FILES["src/writer.rs"] + tests},
             {"src/writer.rs": self.LIMIT_MOVED["src/writer.rs"] + tests}),
            ("src/reader.rs: a crate macro the proof cannot read",
             {"src/reader.rs": "macro_rules! globs {\n    () => { use crate::writer::*; };\n}\n\n" + reader},
             {"src/reader.rs": "macro_rules! globs {\n    () => { use crate::writer::*; };\n}\n\n" + moved}),
            ("src/report.rs: a glob use the proof cannot resolve",
             {"src/lib.rs": "mod constants;\nmod helpers;\nmod reader;\nmod report;\nmod writer;\n",
              "src/report.rs": "use crate::writer as tools;\nuse tools::*;\n\n"
                               + "pub(crate) fn report() -> usize { write() }\n"},
             {}),
        )
        for reason, before, after in cases:
            with self.subTest(reason=reason):
                self.assert_move_refused(reason, {**self.GLOB_FILES, **before}, {**self.GLOB_MOVED, **after})

    def test_a_move_between_files_that_glob_the_same_crate_is_proven_and_refused_when_only_one_does(self) -> None:
        glob = "use std::fmt::*;\n\n"
        files = ("src/writer.rs", "src/constants.rs")
        before = {**self.LIMIT_FILES, **{path: glob + self.LIMIT_FILES[path] for path in files}}
        after = {**self.LIMIT_MOVED, **{path: glob + self.LIMIT_MOVED[path] for path in files}}
        result = self.prove_move(before, after)
        assert isinstance(result, style_footprint.ItemMoves), result
        self.assert_move_refused("src/writer.rs and src/constants.rs glob different modules or crates",
                                 {**before, "src/constants.rs": self.LIMIT_FILES["src/constants.rs"]},
                                 {**after, "src/constants.rs": self.LIMIT_MOVED["src/constants.rs"]})

    P635_FILES: dict[str, str] = {
        "src/lib.rs": "mod reader;\nmod writer;\n\npub use writer::LIMIT;\n",
        "src/writer.rs": "/// The most rows one write takes.\npub const LIMIT: usize = 4;\n\n"
                         + "pub(crate) fn write() -> usize { LIMIT + 1 }\n",
        "src/reader.rs": "use crate::writer::LIMIT;\n\npub(crate) fn read() -> usize { LIMIT }\n",
    }
    P635_MOVED: dict[str, str] = {
        "src/lib.rs": "mod constants;\nmod reader;\nmod writer;\n\npub use constants::LIMIT;\n",
        "src/constants.rs": "/// The most rows one write takes.\npub const LIMIT: usize = 4;\n",
        "src/writer.rs": "use crate::constants::LIMIT;\n\npub(crate) fn write() -> usize { LIMIT + 1 }\n",
        "src/reader.rs": "use crate::constants::LIMIT;\n\npub(crate) fn read() -> usize { LIMIT }\n",
    }

    def judge_move(self, before: dict[str, str], after: dict[str, str]
                   ) -> tuple[style_footprint.ItemMoves | style_footprint.NoItemMoves,
                              style_footprint.Mechanical | style_footprint.Review]:
        result = self.prove_move(before, after)
        changes = style_footprint.changes_between(self.repo, "HEAD~1", "HEAD")
        finding = style_footprint.Finding(style_footprint.WholeDiff(), changes)
        return result, style_footprint.judge(finding, style_footprint.RevisionTree(self.repo, "HEAD~1"))

    def test_a_const_moved_to_a_new_constants_module_is_judged_mechanical(self) -> None:
        moves, verdict = self.judge_move(self.P635_FILES, self.P635_MOVED)
        self.assertIsInstance(moves, style_footprint.ItemMoves, moves)
        self.assertEqual(verdict, style_footprint.Mechanical())

    def test_a_const_moved_with_a_changed_literal_is_judged_for_review(self) -> None:
        # The proof refuses, so judge gives the verdict it gave before it read item moves.
        after = {**self.P635_MOVED, "src/constants.rs": self.P635_MOVED["src/constants.rs"].replace("= 4;", "= 5;")}
        moves, verdict = self.judge_move(self.P635_FILES, after)
        self.assertIsInstance(moves, style_footprint.NoItemMoves, moves)
        self.assertEqual(verdict, style_footprint.Review((
            "check 1: file or module layout changes", "check 2: externally reachable item changes",
            "check 6: condition, literal or control flow flagged",
            "check 6: serialized, reflected, configured or documented name flagged",
            "check 6: call target, method or other change not proven equivalent")))

    def test_a_move_beside_another_edit_is_judged_for_review(self) -> None:
        reader = self.P635_MOVED["src/reader.rs"].replace("{ LIMIT }", "{ LIMIT + 2 }")
        after = {**self.P635_MOVED, "src/reader.rs": reader}
        moves, verdict = self.judge_move(self.P635_FILES, after)
        self.assertIsInstance(moves, style_footprint.NoItemMoves, moves)
        assert isinstance(verdict, style_footprint.Review), verdict
        self.assertIn("check 6: condition, literal or control flow flagged", verdict.reasons)

    def test_a_const_move_of_more_lines_than_the_size_limit_is_judged_mechanical(self) -> None:
        value = "= 0" + "\n    + 1" * style_footprint.FILE_CHANGED_LINE_LIMIT + ";"
        before = {path: text.replace("= 4;", value) for path, text in self.P635_FILES.items()}
        after = {path: text.replace("= 4;", value) for path, text in self.P635_MOVED.items()}
        moves, verdict = self.judge_move(before, after)
        self.assertIsInstance(moves, style_footprint.ItemMoves, moves)
        self.assertEqual(verdict, style_footprint.Mechanical())

    def test_the_check_command_passes_a_proven_const_move(self) -> None:
        _ = self.prove_move(self.P635_FILES, self.P635_MOVED)
        result = self.check("HEAD~1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
