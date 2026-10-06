"""The function length hook mirrors clippy in synthetic Rust packages."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast, override

import fn_length_lib


HOOK = Path(__file__).with_name("post-tool-use-fn-length.py")
SETTINGS = HOOK.parent.parent.parent / "settings.json"


def rust_function(name: str, lines: int) -> str:
    return f"fn {name}() {{\n" + "    work();\n" * lines + "}\n"


class FunctionCountingTests(unittest.TestCase):
    def test_blank_and_comment_only_lines_do_not_count(self) -> None:
        body = "\n\n  // comment\n  /* comment */\n  work();\n\n"
        self.assertEqual(fn_length_lib.count_body_lines(body), 1)

    def test_multiline_block_comment_and_code_after_it(self) -> None:
        body = "/* first\n still comment\n end */ work();\n/* c */ code();\ncode(); // c"
        self.assertEqual(fn_length_lib.count_body_lines(body), 3)

    def test_string_with_slashes_is_counted(self) -> None:
        self.assertEqual(fn_length_lib.count_body_lines('"http://x"'), 1)

    def test_one_line_body_counts_one_line(self) -> None:
        self.assertEqual(fn_length_lib.count_body_lines(" x "), 1)


class FunctionDiscoveryTests(unittest.TestCase):
    def measured(self, source: str) -> list[fn_length_lib.FunctionLength]:
        return fn_length_lib.measure_functions(source)

    def test_nested_function_is_measured_and_counts_in_parent(self) -> None:
        source = "fn outer() {\n    work();\n    fn inner() {\n        work();\n    }\n}\n"
        functions = self.measured(source)
        self.assertEqual([(item.name, item.line, item.lines) for item in functions],
                         [("outer", 1, 4), ("inner", 3, 1)])

    def test_trait_declaration_function_pointer_and_closure_are_skipped(self) -> None:
        source = (
            "trait T { fn declared(&self); }\n"
            "type Callback = fn(u8) -> u8;\n"
            "fn real() {\n"
            "    let callback = || { work(); };\n"
            "}\n"
        )
        functions = self.measured(source)
        self.assertEqual([(item.name, item.line, item.lines) for item in functions],
                         [("real", 3, 1)])

    def test_uninvoked_macro_definition_and_foreign_invocation_are_skipped(self) -> None:
        """Uninvoked definitions and invocation bodies create no functions."""
        source = (
            "macro_rules! make { () => { fn hidden() { work(); } } }\n"
            "foreign! { fn also_hidden() { work(); } }\n"
            "fn visible() { work(); }\n"
        )
        self.assertEqual([item.name for item in self.measured(source)], ["visible"])

    def test_locally_invoked_macro_definition_does_not_expose_functions(self) -> None:
        """A same-file call does not make macro body functions measurable."""
        source = (
            "macro_rules! make {\n"
            "    () => {\n"
            "        impl Thing {\n"
            "            fn new() { work(); }\n"
            "            fn default() { work(); }\n"
            "        }\n"
            "    };\n"
            "}\n"
            "make!();\n"
            "fn visible() { work(); }\n"
        )
        self.assertEqual(
            [(item.name, item.line, item.lines) for item in self.measured(source)],
            [("visible", 10, 1)],
        )

    def test_braces_in_literals_and_nested_comments_do_not_close_body(self) -> None:
        source = (
            "fn balanced<'a>(value: &'a str) {\n"
            "    let plain = \"}\";\n"
            "    let raw = r##\"{ }\"##;\n"
            "    let byte = b\"}\";\n"
            "    let character = '{';\n"
            "    let borrowed: &'a str = value;\n"
            "    /* outer { /* inner } */ outer } */\n"
            "    work();\n"
            "}\n"
            "fn after() { work(); }\n"
        )
        self.assertEqual([(item.name, item.line) for item in self.measured(source)],
                         [("balanced", 1), ("after", 10)])

    def test_unclosed_function_is_dropped_without_losing_prior_function(self) -> None:
        source = "fn complete() { work(); }\nfn unclosed() {\n    work();\n"
        self.assertEqual([item.name for item in self.measured(source)], ["complete"])

    def test_function_attributes_exempt_only_relevant_lints(self) -> None:
        cases = (
            ("#[allow(clippy::too_many_lines)]", True),
            ('#[expect(\n    clippy::too_many_lines,\n    reason = "reviewed"\n)]', True),
            ("#[allow(clippy::pedantic)]", True),
            ("#[cfg_attr(test, allow(clippy::too_many_lines))]", True),
            ("#[allow(clippy::too_many_arguments)]", False),
        )
        for attribute, exempt in cases:
            with self.subTest(attribute=attribute):
                functions = self.measured(attribute + "\n" + rust_function("subject", 1))
                self.assertEqual(len(functions), 1)
                self.assertEqual(functions[0].exempt, exempt)

    def test_impl_and_module_attributes_exempt_contained_functions(self) -> None:
        cases = (
            "#[allow(clippy::too_many_lines)]\nimpl Thing {\nfn subject() { work(); }\n}\n",
            "#[allow(clippy::too_many_lines)]\nmod tests {\nfn subject() { work(); }\n}\n",
            "#[allow(clippy::pedantic)]\ntrait Thing {\nfn subject() { work(); }\n}\n",
        )
        for source in cases:
            with self.subTest(source=source.splitlines()[1]):
                functions = self.measured(source)
                self.assertEqual(len(functions), 1)
                self.assertTrue(functions[0].exempt)

    def test_file_inner_attribute_exempts_following_function(self) -> None:
        source = "#![allow(clippy::too_many_lines)]\n" + rust_function("subject", 1)
        functions = self.measured(source)
        self.assertEqual(len(functions), 1)
        self.assertTrue(functions[0].exempt)

    def test_inner_attribute_exempts_only_its_module(self) -> None:
        source = (
            "mod tests {\n#![allow(clippy::too_many_lines)]\n"
            "fn inside() { work(); }\n}\n"
            "fn outside() { work(); }\n"
        )
        self.assertEqual([(item.name, item.exempt) for item in self.measured(source)],
                         [("inside", True), ("outside", False)])

    def test_backslash_newline_in_string_preserves_later_function(self) -> None:
        """A string with an escape then backslash-newline leaves the next fn visible."""
        source = 'fn first() { let s = "a\\n\\' + '\n b"; }\nfn later() { x(); }\n'
        self.assertEqual(
            [(item.name, item.line, item.lines) for item in self.measured(source)],
            [("first", 1, 2), ("later", 3, 1)],
        )

    def test_impl_return_type_keeps_outer_exemption_on_nested_function(self) -> None:
        """Impl in a return type does not replace the enclosing item."""
        source = (
            "#[allow(clippy::too_many_lines)]\n"
            "fn outer() -> impl Fn() {\n"
            "    fn inner() { work(); }\n"
            "    || work()\n"
            "}\n"
        )
        self.assertEqual(
            [(item.name, item.exempt) for item in self.measured(source)],
            [("outer", True), ("inner", True)],
        )

    def test_spaced_and_commented_macro_calls_hide_generated_functions(self) -> None:
        """Whitespace and comments before ! keep a call a macro."""
        source = (
            "make ! { fn spaced() { work(); } }\n"
            "make /* comment */ ! { fn commented() { work(); } }\n"
            "fn visible() { work(); }\n"
        )
        self.assertEqual([item.name for item in self.measured(source)], ["visible"])

    def test_not_equal_operator_does_not_hide_nested_or_later_functions(self) -> None:
        """The != operator must not consume the following block as a macro."""
        source = (
            "fn before() {\n"
            "    if left!=right {\n"
            "        fn nested() { work(); }\n"
            "    }\n"
            "}\n"
            "fn after() { work(); }\n"
        )
        self.assertEqual(
            [(item.name, item.line) for item in self.measured(source)],
            [("before", 1), ("nested", 3), ("after", 6)],
        )

    def test_keyword_prefix_bangs_do_not_hide_block_functions(self) -> None:
        """A keyword before a prefix bang cannot name a macro."""
        source = (
            "fn outer() {\n"
            "    if !flag { fn conditional() { work(); } }\n"
            "    while !done { fn repeated() { work(); } }\n"
            "    match !ready { _ => { fn matched() { work(); } } }\n"
            "}\n"
            "fn after() { work(); }\n"
        )
        self.assertEqual(
            [(item.name, item.line) for item in self.measured(source)],
            [("outer", 1), ("conditional", 2), ("repeated", 3),
             ("matched", 4), ("after", 6)],
        )

    def test_string_slashes_before_prefix_bang_do_not_hide_functions(self) -> None:
        """String contents cannot become a line comment during macro detection."""
        source = (
            "fn outer() {\n"
            '    let s = "a//b"; if !flag { fn nested() { work(); } }\n'
            "}\n"
            "fn after() { work(); }\n"
        )
        self.assertEqual(
            [(item.name, item.line) for item in self.measured(source)],
            [("outer", 1), ("nested", 2), ("after", 4)],
        )

    def test_unicode_and_raw_function_names_are_preserved(self) -> None:
        """Rust Unicode and raw identifiers retain their written names."""
        source = "fn café() { work(); }\nfn r#type() { work(); }\n"
        self.assertEqual(
            [(item.name, item.line) for item in self.measured(source)],
            [("café", 1), ("r#type", 2)],
        )


class SyntheticPackageTests(unittest.TestCase):
    root: Path = Path()
    workspace: Path = Path()
    package: Path = Path()
    rs_file: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.workspace = self.root / "workspace"
        self.package = self.workspace / "crate"
        self.rs_file = self.package / "src/lib.rs"
        self.rs_file.parent.mkdir(parents=True)
        _ = (self.workspace / "Cargo.toml").write_text(
            '[workspace]\nmembers = ["crate"]\n' +
            '[workspace.lints.clippy]\npedantic = "deny"\n'
        )
        _ = (self.package / "Cargo.toml").write_text(
            '[package]\nname = "sample"\nversion = "0.1.0"\nedition = "2021"\n' +
            '[lints]\nworkspace = true\n'
        )

    def write_function(self, lines: int) -> None:
        _ = self.rs_file.write_text(rust_function("subject", lines))


class FunctionScopeTests(SyntheticPackageTests):
    def test_unneeded_parser_and_path_modules_are_not_imported(self) -> None:
        """A Rust file with no lint key avoids costly optional imports."""
        _ = (self.package / "Cargo.toml").write_text(
            '[package]\nname = "sample"\nversion = "0.1.0"\n'
        )
        self.write_function(101)
        script = (
            "import sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "import fn_length_lib\n"
            "scope, functions = fn_length_lib.long_functions(sys.argv[2])\n"
            "if scope.enabled or functions:\n"
            "    raise RuntimeError('unexpected lint scope')\n"
            "print(','.join(name for name in ('dataclasses', 'tomllib', 'pathlib') "
            "if name in sys.modules))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script, str(HOOK.parent), str(self.rs_file)],
            capture_output=True, text=True, check=False, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "\n")

    def test_cfg_test_module_in_example_does_not_block_edit(self) -> None:
        """An example build omits tests even when their allow names another lint."""
        example = self.package / "examples/widgets.rs"
        example.parent.mkdir()
        _ = example.write_text(
            "#[cfg(test)]\n#[allow(clippy::expect_used)]\nmod tests {\n" +
            rust_function("test_layout", 101) + "}\n"
        )
        measured = fn_length_lib.measure_functions(example.read_text())
        self.assertEqual([(item.name, item.lines, item.exempt) for item in measured],
                         [("test_layout", 101, False)])
        self.assertEqual(fn_length_lib.long_functions(example)[1], [])

    def test_library_module_named_examples_still_checks_tests(self) -> None:
        """A library directory named examples does not change its lint scope."""
        library_module = self.package / "src/examples/helpers.rs"
        library_module.parent.mkdir()
        _ = library_module.write_text(
            "#[cfg(test)]\nmod tests {\n" + rust_function("long_test", 101) + "}\n"
        )
        _, long = fn_length_lib.long_functions(library_module)
        self.assertEqual([(item.name, item.lines) for item in long],
                         [("long_test", 101)])

    def test_stacked_attributes_exempt_a_module_with_long_functions(self) -> None:
        """Every outer attribute attaches to the following module."""
        source = (
            "#[cfg(test)]\n" +
            "#[allow(\n" +
            "    clippy::expect_used,\n" +
            "    clippy::panic,\n" +
            "    clippy::too_many_lines,\n" +
            '    reason = "tests should panic"\n' +
            ")]\n" +
            "mod tests {\n" +
            rust_function("long_test", 101)
            + "}\n"
        )
        _ = self.rs_file.write_text(source)
        measured = fn_length_lib.measure_functions(source)
        self.assertEqual([(item.name, item.exempt) for item in measured],
                         [("long_test", True)])
        self.assertEqual(fn_length_lib.long_functions(self.rs_file)[1], [])

    def test_default_boundary_is_100_counted_lines(self) -> None:
        self.write_function(100)
        scope, functions = fn_length_lib.long_functions(self.rs_file)
        self.assertTrue(scope.enabled)
        self.assertEqual(scope.threshold, 100)
        self.assertEqual(functions, [])
        self.write_function(101)
        _, functions = fn_length_lib.long_functions(self.rs_file)
        self.assertEqual([(item.name, item.lines) for item in functions], [("subject", 101)])

    def test_clippy_toml_threshold_moves_boundary(self) -> None:
        _ = (self.workspace / "clippy.toml").write_text("too-many-lines-threshold = 50\n")
        self.write_function(50)
        scope, functions = fn_length_lib.long_functions(self.rs_file)
        self.assertEqual(scope.threshold, 50)
        self.assertEqual(functions, [])
        self.write_function(51)
        _, functions = fn_length_lib.long_functions(self.rs_file)
        self.assertEqual([item.lines for item in functions], [51])

    def test_explicit_too_many_lines_allow_overrides_pedantic_deny(self) -> None:
        _ = (self.workspace / "Cargo.toml").write_text(
            '[workspace]\nmembers = ["crate"]\n' +
            '[workspace.lints.clippy]\npedantic = "deny"\ntoo_many_lines = "allow"\n'
        )
        self.write_function(101)
        scope, functions = fn_length_lib.long_functions(self.rs_file)
        self.assertFalse(scope.enabled)
        self.assertEqual(functions, [])

    def test_missing_lints_disables_scope(self) -> None:
        _ = (self.package / "Cargo.toml").write_text(
            '[package]\nname = "sample"\nversion = "0.1.0"\n'
        )
        self.write_function(101)
        self.assertFalse(fn_length_lib.lint_scope(self.rs_file).enabled)

    def test_missing_cargo_manifest_disables_scope(self) -> None:
        (self.package / "Cargo.toml").unlink()
        (self.workspace / "Cargo.toml").unlink()
        self.write_function(101)
        self.assertFalse(fn_length_lib.lint_scope(self.rs_file).enabled)

    def test_malformed_manifest_disables_scope(self) -> None:
        _ = (self.package / "Cargo.toml").write_text("[package\n")
        self.write_function(101)
        self.assertFalse(fn_length_lib.lint_scope(self.rs_file).enabled)

    def test_non_workspace_package_lints_enable_scope(self) -> None:
        _ = (self.package / "Cargo.toml").write_text(
            '[package]\nname = "sample"\nversion = "0.1.0"\n' +
            '[lints.clippy]\ntoo_many_lines = { level = "warn" }\n'
        )
        self.write_function(101)
        self.assertTrue(fn_length_lib.lint_scope(self.rs_file).enabled)


class FunctionHookTests(SyntheticPackageTests):
    environment: dict[str, str] = {}
    state: Path = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
        self.state = self.root / "state"
        self.environment = {
            **os.environ,
            "HOME": str(self.root / "home"),
            "FN_LENGTH_HOOK_STATE": str(self.state),
        }

    def run_hook(self, tool: str = "Edit", *, file: Path | None = None,
                 payload: str | None = None) -> subprocess.CompletedProcess[str]:
        if payload is None:
            payload = json.dumps({
                "tool_name": tool,
                "tool_input": {"file_path": str(file or self.rs_file)},
                "cwd": str(self.workspace),
            })
        return subprocess.run(
            [sys.executable, str(HOOK)], input=payload, capture_output=True,
            text=True, check=False, env=self.environment, timeout=10,
        )

    def test_edit_blocks_with_exact_reason_and_logs_claude(self) -> None:
        self.write_function(101)
        result = self.run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        decision = cast(dict[str, object], json.loads(result.stdout))
        self.assertEqual(decision["decision"], "block")
        self.assertEqual(decision["reason"],
                         "fn subject at crate/src/lib.rs:1 is 101 lines (limit 100): " +
                         "split it now. The edit was applied.")
        self.assertEqual(decision["continue"], True)
        self.assertEqual(decision["systemMessage"], "fn-length: lib.rs has 1 function(s) over 100 lines")
        context = cast(dict[str, str], decision["hookSpecificOutput"])
        self.assertEqual(context["hookEventName"], "PostToolUse")
        self.assertIn("split each into named helpers", context["additionalContext"].lower())
        records = (self.state / "blocks.jsonl").read_text().splitlines()
        self.assertEqual(len(records), 1)
        record = cast(dict[str, object], json.loads(records[0]))
        self.assertEqual(record["agent"], "claude")
        self.assertEqual(record["tool"], "Edit")
        self.assertEqual(record["cwd"], str(self.workspace))
        self.assertEqual(record["file"], str(self.rs_file))
        self.assertEqual(record["threshold"], 100)
        self.assertEqual(record["functions"], [{"name": "subject", "line": 1, "lines": 101}])

    def test_write_and_multiedit_block(self) -> None:
        self.write_function(101)
        for tool in ("Write", "MultiEdit"):
            with self.subTest(tool=tool):
                result = self.run_hook(tool)
                self.assertEqual(result.returncode, 0, result.stderr)
                decision = cast(dict[str, object], json.loads(result.stdout))
                self.assertEqual(decision["decision"], "block")

    def test_one_edit_reports_every_long_function_in_source_order(self) -> None:
        _ = self.rs_file.write_text(rust_function("first", 101) + rust_function("second", 102))
        result = self.run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        decision = cast(dict[str, object], json.loads(result.stdout))
        self.assertEqual(
            decision["reason"],
            "fn first at crate/src/lib.rs:1 is 101 lines (limit 100); " +
            "fn second at crate/src/lib.rs:104 is 102 lines (limit 100): " +
            "split it now. The edit was applied.",
        )

    def test_short_function_passes_without_output_or_log(self) -> None:
        self.write_function(100)
        result = self.run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertFalse((self.state / "blocks.jsonl").exists())

    def test_non_rust_path_passes_without_log(self) -> None:
        other = self.package / "src/lib.py"
        _ = other.write_text("pass\n")
        result = self.run_hook(file=other)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertFalse((self.state / "blocks.jsonl").exists())

    def test_malformed_stdin_has_no_block_decision(self) -> None:
        result = self.run_hook(payload="{")
        self.assertEqual(result.returncode, 0, result.stderr)
        if result.stdout:
            decision = cast(dict[str, object], json.loads(result.stdout))
            self.assertNotEqual(decision.get("decision"), "block")

    def test_settings_registers_hook_after_basedpyright(self) -> None:
        settings = cast(dict[str, object], json.loads(SETTINGS.read_text()))
        hooks = cast(dict[str, list[dict[str, object]]], settings["hooks"])
        groups = [group for group in hooks["PostToolUse"]
                  if group.get("matcher") == "Edit|MultiEdit|Write"]
        self.assertEqual(len(groups), 1)
        commands = cast(list[dict[str, object]], groups[0]["hooks"])
        self.assertEqual(
            [cast(str, handler["command"]) for handler in commands[:2]],
            [
                '"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/post-tool-use-basedpyright.py"',
                '"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/post-tool-use-fn-length.py"',
            ],
        )


class CodexApplyPatchTests(SyntheticPackageTests):
    environment: dict[str, str] = {}
    state: Path = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
        self.state = self.root / "state"
        self.environment = {
            **os.environ,
            "HOME": str(self.root / "home"),
            "CODEX_HOME": str(self.root / "codex"),
            "FN_LENGTH_HOOK_STATE": str(self.state),
        }

    def run_patch(self, patch: str) -> subprocess.CompletedProcess[str]:
        payload = {
            "tool_name": "apply_patch",
            "cwd": str(self.workspace),
            "tool_input": {"command": patch},
        }
        return subprocess.run(
            [sys.executable, str(HOOK)], input=json.dumps(payload),
            capture_output=True, text=True, check=False,
            env=self.environment, timeout=10,
        )

    def records(self) -> list[dict[str, object]]:
        return [cast(dict[str, object], json.loads(line)) for line in
                (self.state / "blocks.jsonl").read_text().splitlines()]

    def test_update_file_blocks_and_logs_codex(self) -> None:
        self.write_function(101)
        result = self.run_patch("*** Begin Patch\n*** Update File: crate/src/lib.rs\n@@\n*** End Patch\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        decision = cast(dict[str, object], json.loads(result.stdout))
        self.assertEqual(decision["decision"], "block")
        self.assertIn("fn subject at crate/src/lib.rs:1 is 101 lines (limit 100)",
                      cast(str, decision["reason"]))
        self.assertIn("The edit was applied.", cast(str, decision["reason"]))
        self.assertEqual(self.records()[0]["agent"], "codex")

    def test_add_file_resolves_against_payload_cwd(self) -> None:
        added = self.package / "src/added.rs"
        _ = added.write_text(rust_function("added", 101))
        result = self.run_patch("*** Begin Patch\n*** Add File: crate/src/added.rs\n*** End Patch\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        decision = cast(dict[str, object], json.loads(result.stdout))
        self.assertIn("fn added at crate/src/added.rs:1", cast(str, decision["reason"]))
        self.assertEqual(self.records()[0]["file"], str(added))

    def test_update_then_move_checks_destination(self) -> None:
        moved = self.package / "src/moved.rs"
        _ = moved.write_text(rust_function("moved", 101))
        result = self.run_patch(
            "*** Begin Patch\n*** Update File: crate/src/old.rs\n" +
            "*** Move to: crate/src/moved.rs\n@@\n*** End Patch\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        decision = cast(dict[str, object], json.loads(result.stdout))
        self.assertIn("fn moved at crate/src/moved.rs:1", cast(str, decision["reason"]))
        self.assertEqual(self.records()[0]["file"], str(moved))

    def test_delete_file_only_passes_without_log(self) -> None:
        self.write_function(101)
        result = self.run_patch("*** Begin Patch\n*** Delete File: crate/src/lib.rs\n*** End Patch\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertFalse((self.state / "blocks.jsonl").exists())

    def test_non_rust_patch_passes_without_log(self) -> None:
        _ = (self.package / "README.md").write_text("notes\n")
        result = self.run_patch("*** Begin Patch\n*** Update File: crate/README.md\n@@\n*** End Patch\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertFalse((self.state / "blocks.jsonl").exists())

    def test_two_rust_files_make_one_block_and_two_records(self) -> None:
        self.write_function(101)
        other = self.package / "src/other.rs"
        _ = other.write_text(rust_function("other", 102))
        result = self.run_patch(
            "*** Begin Patch\n*** Update File: crate/src/lib.rs\n@@\n" +
            "*** Update File: crate/src/other.rs\n@@\n*** End Patch\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 1)
        decision = cast(dict[str, object], json.loads(result.stdout))
        self.assertEqual(decision["decision"], "block")
        reason = cast(str, decision["reason"])
        self.assertIn("fn subject at crate/src/lib.rs:1", reason)
        self.assertIn("fn other at crate/src/other.rs:1", reason)
        self.assertIn("The edit was applied.", reason)
        records = self.records()
        self.assertEqual(len(records), 2)
        self.assertEqual({record["file"] for record in records}, {str(self.rs_file), str(other)})
        self.assertEqual({record["agent"] for record in records}, {"codex"})

    def test_patch_summary_counts_all_functions_and_names_all_files(self) -> None:
        """A patch summary covers every long function and affected file."""
        _ = self.rs_file.write_text(rust_function("first", 101) + rust_function("second", 102))
        other = self.package / "src/other.rs"
        _ = other.write_text(rust_function("third", 103))
        result = self.run_patch(
            "*** Begin Patch\n*** Update File: crate/src/lib.rs\n@@\n" +
            "*** Update File: crate/src/other.rs\n@@\n*** End Patch\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        decision = cast(dict[str, object], json.loads(result.stdout))
        self.assertEqual(
            decision["systemMessage"],
            "fn-length: 3 function(s) over 100 lines in crate/src/lib.rs, crate/src/other.rs",
        )

    def test_patch_summary_names_each_files_threshold(self) -> None:
        """Differing package limits are both visible in one patch summary."""
        self.write_function(101)
        other_package = self.workspace / "other"
        other_file = other_package / "src/other.rs"
        other_file.parent.mkdir(parents=True)
        _ = (other_package / "Cargo.toml").write_text(
            '[package]\nname = "other"\nversion = "0.1.0"\n' +
            '[lints.clippy]\npedantic = "deny"\n'
        )
        _ = (other_package / "clippy.toml").write_text("too-many-lines-threshold = 120\n")
        _ = other_file.write_text(rust_function("other", 121))
        result = self.run_patch(
            "*** Begin Patch\n*** Update File: crate/src/lib.rs\n@@\n" +
            "*** Update File: other/src/other.rs\n@@\n*** End Patch\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        decision = cast(dict[str, object], json.loads(result.stdout))
        self.assertEqual(
            decision["systemMessage"],
            "fn-length: 2 function(s) over limits: " +
            "1 over 100 lines in crate/src/lib.rs; 1 over 120 lines in other/src/other.rs",
        )


if __name__ == "__main__":
    _ = unittest.main()
