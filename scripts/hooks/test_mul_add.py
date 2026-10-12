"""The mul_add hook checks likely multiply-add findings in temporary crates."""

from __future__ import annotations

import json
import io
import os
import runpy
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Callable, cast, override
from unittest.mock import patch

import fn_length_lib
import mul_add_lib


HOOK = Path(__file__).with_name("post-tool-use-mul-add.py")
SETTINGS = HOOK.parent.parent.parent / "settings.json"


class MulAddHookTests(unittest.TestCase):
    root: Path = Path()
    workspace: Path = Path()
    package: Path = Path()
    rs_file: Path = Path()
    state: Path = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.workspace = self.root / "workspace"
        self.package = self.workspace / "crate"
        self.rs_file = self.package / "src/lib.rs"
        self.rs_file.parent.mkdir(parents=True)
        _ = (self.workspace / "Cargo.toml").write_text(
            '[workspace]\nmembers = ["crate"]\n' +
            '[workspace.lints.clippy]\nnursery = "deny"\n'
        )
        _ = (self.package / "Cargo.toml").write_text(
            '[package]\nname = "sample"\nversion = "0.1.0"\nedition = "2021"\n' +
            '[lints]\nworkspace = true\n'
        )
        self.state = self.root / "state"
        self.environment = {
            **os.environ,
            "HOME": str(self.root / "home"),
            "MUL_ADD_HOOK_STATE": str(self.state),
        }

    def write_source(self, expression: str) -> None:
        _ = self.rs_file.write_text(
            "fn sample(a: f32, c: f32, mut x: f32) {\n" +
            f"    let _ = {expression};\n" +
            "}\n"
        )

    def run_hook(
        self, tool: str = "Write", *, file: Path | None = None, payload: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        if payload is None:
            target = file or self.rs_file
            source = target.read_text()
            inputs: dict[str, object] = {"file_path": str(target)}
            if tool == "Write":
                inputs["content"] = source
            elif tool == "MultiEdit":
                inputs["edits"] = [{"old_string": "", "new_string": source}]
            else:
                inputs.update({"old_string": "", "new_string": source})
            payload = json.dumps({
                "tool_name": tool,
                "tool_input": inputs,
                "cwd": str(self.workspace),
            })
        return subprocess.run(
            [sys.executable, str(HOOK)], input=payload, capture_output=True,
            text=True, check=False, env=self.environment, timeout=10,
        )

    def decision(self, tool: str = "Write", *, payload: str | None = None) -> dict[str, object]:
        result = self.run_hook(tool, payload=payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout, "hook passed without reporting the expected sum")
        decision = cast(dict[str, object], json.loads(result.stdout))
        self.assertEqual(decision["decision"], "block")
        self.assertEqual(decision["continue"], True)
        return decision

    def edit_payload(self, tool: str, inputs: dict[str, object]) -> str:
        return json.dumps({
            "tool_name": tool,
            "tool_input": {"file_path": str(self.rs_file), **inputs},
            "cwd": str(self.workspace),
        })

    def assert_passes(self, *, file: Path | None = None, payload: str | None = None) -> None:
        result = self.run_hook(file=file, payload=payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        if result.stdout:
            output = cast(dict[str, object], json.loads(result.stdout))
            self.assertNotEqual(output.get("decision"), "block")
            self.assertEqual(len(result.stdout.splitlines()), 1)
        self.assertFalse((self.state / "blocks.jsonl").exists())

    def assert_reported(self, expression: str, rewrite: str, *, line: int = 2) -> None:
        decision = self.decision()
        description = (f"crate/src/lib.rs:{line}: if {expression} is f32 or f64 arithmetic, "
                       + f"write {rewrite} (clippy::suboptimal_flops)")
        self.assertEqual(decision["reason"], description
                         + ". Leave integer and vector arithmetic as it is. The edit was applied.")
        context = cast(dict[str, str], decision["hookSpecificOutput"])
        self.assertEqual(context["additionalContext"], description)

    def assert_quiet(self) -> None:
        result = self.run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertFalse((self.state / "blocks.jsonl").exists())

    def test_each_binary_and_compound_shape_has_clippys_rewrite(self) -> None:
        cases = (
            ("a * 0.5 + c", "a.mul_add(0.5, c)"),
            ("c + a * 0.5", "a.mul_add(0.5, c)"),
            ("a * 0.5 - c", "a.mul_add(0.5, -c)"),
            ("c - a * 0.5", "a.mul_add(-0.5, c)"),
            ("x += a * 0.5", "x = a.mul_add(0.5, x)"),
            ("x -= a * 0.5", "x = a.mul_add(-0.5, x)"),
        )
        for expression, rewrite in cases:
            with self.subTest(expression=expression):
                self.write_source(expression)
                self.assert_reported(expression, rewrite)

    def test_add_assignment_with_additive_rhs_reports_the_rhs_sum(self) -> None:
        self.write_source("x += a * b + c")
        self.assert_reported("a * b + c", "a.mul_add(b, c)")
        record = cast(dict[str, object], json.loads((self.state / "blocks.jsonl").read_text()))
        self.assertEqual(record["expression"], "a * b + c")

    def test_subtract_assignment_with_additive_rhs_reports_the_rhs_sum(self) -> None:
        self.write_source("x -= a * b + c")
        self.assert_reported("a * b + c", "a.mul_add(b, c)")
        record = cast(dict[str, object], json.loads((self.state / "blocks.jsonl").read_text()))
        self.assertEqual(record["expression"], "a * b + c")

    def test_nontrivial_receiver_is_parenthesized(self) -> None:
        self.write_source("(a + c) * 0.5 + c")
        self.assertIn("(a + c).mul_add(0.5, c)", cast(str, self.decision()["reason"]))

    def test_float_literal_spellings_supply_float_evidence(self) -> None:
        for literal in ("1.0", "0.5_f32", "2f64", "1e-5"):
            with self.subTest(literal=literal):
                float_type = "f64" if literal == "2f64" else "f32"
                _ = self.rs_file.write_text(
                    f"fn sample(a: {float_type}, c: {float_type}) {{\n" +
                    f"    let _ = a * 3 + {literal};\n" +
                    "}\n"
                )
                self.assert_reported(f"a * 3 + {literal}", f"a.mul_add(3, {literal})")

    def test_unknown_field_operand_is_reported(self) -> None:
        expression = "metrics.button.height * 0.5 + offset"
        self.write_source(expression)
        self.assert_reported(expression, "metrics.button.height.mul_add(0.5, offset)")

    def test_path_operand_is_reported(self) -> None:
        expression = "hana_organon::CUBE_SIZE * 0.5 + pad"
        self.write_source(expression)
        self.assert_reported(expression, "hana_organon::CUBE_SIZE.mul_add(0.5, pad)")

    def test_call_operand_is_reported(self) -> None:
        expression = "bounds.height() * 0.5 + pad"
        self.write_source(expression)
        self.assert_reported(expression, "bounds.height().mul_add(0.5, pad)")

    def test_index_operand_is_reported_without_integer_evidence(self) -> None:
        expression = "heights[0] * scale + pad"
        self.write_source(expression)
        self.assert_reported(expression, "heights[0].mul_add(scale, pad)")

    def test_field_addend_is_reported(self) -> None:
        expression = "control.min.y + 32.0 * scale"
        self.write_source(expression)
        self.assert_reported(expression, "scale.mul_add(32.0, control.min.y)")

    def test_method_field_addend_is_reported(self) -> None:
        expression = "bounds.centre().y - bounds.height() * 0.5"
        self.write_source(expression)
        self.assert_reported(expression, "bounds.height().mul_add(-0.5, bounds.centre().y)")

    def test_cast_receiver_is_reported_and_parenthesized(self) -> None:
        expression = "index as f32 * 54.0 + 28.0"
        self.write_source(expression)
        self.assert_reported(expression, "(index as f32).mul_add(54.0, 28.0)")

    def test_assert_macro_argument_reports_the_sum_inside_the_comparison(self) -> None:
        _ = self.rs_file.write_text(
            "fn sample() {\n    assert!(x > first.y + row_height() * 0.5);\n}\n"
        )
        self.assert_reported("first.y + row_height() * 0.5", "row_height().mul_add(0.5, first.y)")

    def test_unknown_names_without_float_literals_are_reported(self) -> None:
        expression = "clip.x - whole_edge * clip.w"
        self.write_source(expression)
        self.assert_reported(expression, "whole_edge.mul_add(-clip.w, clip.x)")

    def test_both_unknown_products_give_one_sum_with_the_left_receiver(self) -> None:
        expression = "CUBE_SIZE * 2.0 + GAP * 2.0"
        self.write_source(expression)
        self.assert_reported(expression, "CUBE_SIZE.mul_add(2.0, GAP * 2.0)")

    def test_product_chain_uses_its_last_factor_as_the_mul_add_argument(self) -> None:
        expression = "a * b * c + d"
        self.write_source(expression)
        self.assert_reported(expression, "(a * b).mul_add(c, d)")

    def test_multiline_sum_collapses_whitespace_in_expression_and_rewrite(self) -> None:
        self.write_source("a  *  scale\n        +   offset")
        self.assert_reported("a * scale + offset", "a.mul_add(scale, offset)")

    def test_grouped_product_chain_receiver_keeps_its_full_grouping(self) -> None:
        cases = (
            ("(a + b) * (c + d) * e + f", "((a + b) * (c + d)).mul_add(e, f)"),
            ("(a + b) * c + d", "(a + b).mul_add(c, d)"),
        )
        for expression, rewrite in cases:
            with self.subTest(expression=expression):
                self.write_source(expression)
                self.assert_reported(expression, rewrite)

    def test_generic_calls_keep_their_callee_and_comparisons_end_the_sum(self) -> None:
        cases = (
            ("load::<f32>() * scale + offset", "load::<f32>().mul_add(scale, offset)"),
            ("conv::<f32>(a) * b + c", "conv::<f32>(a).mul_add(b, c)"),
            ("conv::<f32>(3) * b + c", "conv::<f32>(3).mul_add(b, c)"),
            ("values.iter().sum::<f32>() * 0.5 + bias",
             "values.iter().sum::<f32>().mul_add(0.5, bias)"),
        )
        for expression, rewrite in cases:
            with self.subTest(expression=expression):
                self.write_source(expression)
                self.assert_reported(expression, rewrite)
        self.write_source("a * b + c < d")
        self.assert_reported("a * b + c", "a.mul_add(b, c)")

    def test_expression_and_rewrite_preserve_literals_and_remove_comments(self) -> None:
        literals = ('"two  spaces"', 'r#"two  spaces"#', 'b"two  spaces"',
                    'br#"two  spaces"#', "' '", "b' '", 'r#"two\n  lines"#')
        for literal in literals:
            with self.subTest(literal=literal):
                expression = f"lookup({literal}) * scale + offset"
                self.write_source(expression)
                self.assert_reported(expression, f"lookup({literal}).mul_add(scale, offset)")
        cases = (
            ("a * b // gain\n        + c", "a * b + c", "a.mul_add(b, c)"),
            ("(a // keep the sum\n        + b) * c + d", "(a + b) * c + d",
             "(a + b).mul_add(c, d)"),
            ("a /* gain */ * b + c", "a * b + c", "a.mul_add(b, c)"),
            ('lookup("two  spaces // literal") * scale + offset',
             'lookup("two  spaces // literal") * scale + offset',
             'lookup("two  spaces // literal").mul_add(scale, offset)'),
        )
        for source, expression, rewrite in cases:
            with self.subTest(source=source):
                self.write_source(source)
                self.assert_reported(expression, rewrite)

    def test_name_evidence_uses_prior_bindings_in_its_own_function(self) -> None:
        with self.subTest(binding="later scalar shadow"):
            _ = self.rs_file.write_text(
                "fn sample(v: Vec2) {\n    let _ = v * 0.5 + make_offset();\n"
                + "    let v: f32 = 1.0;\n    let _ = v;\n}\n"
            )
            self.assert_quiet()
        sources = (
            ("fn sample(v: f32, offset: f32) {\n"
             + "    fn nested() { let v: Vec2 = Vec2::ZERO; let _ = v; }\n"
             + "    let _ = v * 0.5 + offset;\n}\n", 3),
            ("fn sample(v: Vec2) {\n    let v = value();\n"
             + "    let _ = v * 0.5 + offset;\n}\n", 3),
            ("fn sample() {\n    let v = Vec3::new(x, y, z);\n"
             + "    let v = value();\n    let _ = v * 0.5 + offset;\n}\n", 4),
            ("fn sample() {\n    let v: Vec2 = make_vector();\n"
             + "    let v: f32 = value();\n    let _ = v * 0.5 + offset;\n}\n", 4),
        )
        for source, line in sources:
            with self.subTest(source=source):
                _ = self.rs_file.write_text(source)
                self.assert_reported("v * 0.5 + offset", "v.mul_add(0.5, offset)", line=line)

    def test_parenthesized_whole_products_are_reported_without_unwrapping_other_groups(self) -> None:
        for expression in ("(a * b).abs() + c", "a / (b * c) + d", "-(a * b) + c"):
            with self.subTest(expression=expression):
                self.write_source(expression)
                self.assert_quiet()
        cases = (
            ("(a * b) + c", "a.mul_add(b, c)"),
            ("c + (a * b)", "a.mul_add(b, c)"),
            ("c - (a * b)", "a.mul_add(-b, c)"),
            ("(a * b) - c", "a.mul_add(b, -c)"),
            ("c += (a * b)", "c = a.mul_add(b, c)"),
            ("(a * b * d) + c", "(a * b).mul_add(d, c)"),
            ("a * (b * c) + d", "a.mul_add((b * c), d)"),
        )
        for expression, rewrite in cases:
            with self.subTest(expression=expression):
                self.write_source(expression)
                self.assert_reported(expression, rewrite)
        self.write_source("(a * b + d) + c")
        self.assert_reported("a * b + d", "a.mul_add(b, d)")

    def test_static_mut_initializers_are_exempt_and_nonfloat_names_disprove_sums(self) -> None:
        sources = (
            "fn sample() {\n"
            + "    static mut VALUE: f32 = { let v = 2.0; v * 0.5 + 1.0 };\n}\n",
            "static mut VALUE: Vec2 = Vec2::ONE;\n"
            + "fn sample() { let _ = a * 0.5 + VALUE; }\n",
        )
        for source in sources:
            with self.subTest(source=source):
                _ = self.rs_file.write_text(source)
                self.assert_quiet()

    def test_signed_exponent_literals_remain_whole_operands(self) -> None:
        for literal in ("1e-3", "2.5E+4", "1e-3_f32"):
            receiver_rewrite = (f"{literal}.mul_add(dt, t)" if literal.endswith("_f32")
                                else f"dt.mul_add({literal}, t)")
            cases = (
                (f"dt * {literal} + t", f"dt * {literal} + t", f"dt.mul_add({literal}, t)"),
                (f"{literal} * dt + t", f"{literal} * dt + t", receiver_rewrite),
                (f"c + dt * {literal} + t", f"c + dt * {literal}", f"dt.mul_add({literal}, c)"),
            )
            for source, expression, rewrite in cases:
                with self.subTest(source=source):
                    self.write_source(source)
                    self.assert_reported(expression, rewrite)

    def test_whole_index_sums_pass_while_array_and_macro_sums_are_reported(self) -> None:
        for expression in ("grid[r * w + c]", "grid()[r * w + c]",
                           "grid[0][r * w + c]", "grid()?[r * w + c]"):
            with self.subTest(expression=expression):
                self.write_source(expression)
                self.assert_quiet()
        for expression in ("[a * b + c, d]", "[a * b + c; 4]", "vec![a * b + c]"):
            with self.subTest(expression=expression):
                self.write_source(expression)
                self.assert_reported("a * b + c", "a.mul_add(b, c)")
        self.write_source("grid[a * 0.5 + c]")
        self.assert_reported("a * 0.5 + c", "a.mul_add(0.5, c)")

    def test_only_depth_zero_casts_parenthesize_the_receiver(self) -> None:
        cases = (
            ("lerp(i as f32) * 2.0 + 1.0", "lerp(i as f32).mul_add(2.0, 1.0)"),
            ("i as f32 * 2.0 + 1.0", "(i as f32).mul_add(2.0, 1.0)"),
        )
        for expression, rewrite in cases:
            with self.subTest(expression=expression):
                self.write_source(expression)
                self.assert_reported(expression, rewrite)

    def test_lifetime_and_pointer_types_do_not_exempt_sums_but_static_items_do(self) -> None:
        _ = self.rs_file.write_text(
            "fn sample() {\n"
            + "    static TABLE: f32 = { let a = 2.0; a * 0.5 + 1.0 };\n}\n"
        )
        self.assert_quiet()
        for annotation in ("&'static [f32]", "*const f32"):
            with self.subTest(annotation=annotation):
                _ = self.rs_file.write_text(
                    "fn sample() {\n"
                    + f"    let table: {annotation} = lookup(a * b + c);\n}}\n"
                )
                self.assert_reported("a * b + c", "a.mul_add(b, c)")

    def test_many_findings_scan_scales_with_file_length(self) -> None:
        timings: list[float] = []
        for lines in (200, 800):
            _ = self.rs_file.write_text(
                "fn sample(a: f32, c: f32) {\n" +
                "    let _ = a * 0.5 + c;\n" * lines +
                "}\n"
            )
            samples: list[float] = []
            for _ in range(5):
                started = time.perf_counter()
                _, findings = mul_add_lib.mul_add_findings(self.rs_file)
                samples.append(time.perf_counter() - started)
                self.assertEqual(len(findings), lines)
                self.assertEqual(findings[-1].line, lines + 1)
            timings.append(min(samples))
        # A linear scan is about 4× here, while a quadratic scan is about 16×.
        self.assertLess(timings[1], timings[0] * 10)

    def test_source_without_product_skips_detector_import(self) -> None:
        _ = self.rs_file.write_text("fn sample() {\n" + "    let _ = 1.0;\n" * 248 + "}\n")
        payload = json.dumps({
            "tool_name": "Write",
            "tool_input": {"file_path": str(self.rs_file), "content": self.rs_file.read_text()},
            "cwd": str(self.workspace),
        })
        result = subprocess.run(
            [sys.executable, "-X", "importtime", str(HOOK)], input=payload,
            capture_output=True, text=True, check=False, env=self.environment, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("mul_add_lib", result.stderr)

    def test_main_checks_operators_before_computing_written_lines(self) -> None:
        main = cast(Callable[[], None], runpy.run_path(str(HOOK))["main"])
        for source in ("fn sample() { let _ = 1.0 + 2.0; }\n",
                       "fn sample(a: f32) { let _ = a * 2.0; }\n"):
            with self.subTest(source=source):
                _ = self.rs_file.write_text(source)
                payload = self.edit_payload("Write", {"content": source})
                output = io.StringIO()
                with (patch.dict(os.environ, self.environment),
                      patch.object(sys, "stdin", io.StringIO(payload)), redirect_stdout(output),
                      patch.object(fn_length_lib, "written_lines",
                                   side_effect=AssertionError("unneeded written-line scan")) as scan):
                    main()
                scan.assert_not_called()
                self.assertEqual(output.getvalue(), "")
                self.assertFalse((self.state / "blocks.jsonl").exists())

    def test_non_rust_edit_does_not_import_shared_scanner(self) -> None:
        """The non-Rust fast path avoids scanner and typing imports."""
        payload = json.dumps({
            "tool_name": "Edit", "tool_input": {"file_path": "README.md"},
            "cwd": str(self.workspace),
        })
        result = subprocess.run(
            [sys.executable, "-X", "importtime", str(HOOK)], input=payload,
            capture_output=True, text=True, check=False, env=self.environment, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("fn_length_lib", result.stderr)
        self.assertNotIn("typing", result.stderr)

    def test_finding_carries_source_operands_and_rewrite(self) -> None:
        self.write_source("a * 0.5 + c")
        scope, findings = mul_add_lib.mul_add_findings(self.rs_file)
        self.assertTrue(scope.enabled)
        self.assertEqual(len(findings), 1)
        finding = findings[0]
        self.assertIsInstance(finding, mul_add_lib.MulAddFinding)
        self.assertEqual(
            (finding.line, finding.a, finding.b, finding.c,
             finding.expr, finding.rewrite),
            (2, "a", "0.5", "c", "a * 0.5 + c", "a.mul_add(0.5, c)"),
        )

    def test_integer_literal_factors_and_terms_without_float_evidence_pass(self) -> None:
        for expression in ("i * 3 + 1", "i * width + 1", "1 + i * width"):
            with self.subTest(expression=expression):
                self.write_source(expression)
                self.assert_passes()

    def test_length_or_count_without_float_evidence_passes(self) -> None:
        for expression in ("rows.len() * width + pad", "rows.count() * width + pad"):
            with self.subTest(expression=expression):
                self.write_source(expression)
                self.assert_passes()

    def test_integer_cast_without_float_evidence_passes(self) -> None:
        for integer_type in ("usize", "isize", "u8", "i32", "u128"):
            with self.subTest(integer_type=integer_type):
                self.write_source(f"index as {integer_type} * width + pad")
                self.assert_passes()

    def test_integer_call_argument_is_not_integer_evidence(self) -> None:
        expression = "height(3) * scale + pad"
        self.write_source(expression)
        self.assert_reported(expression, "height(3).mul_add(scale, pad)")

    def test_tuple_field_is_not_integer_evidence(self) -> None:
        expression = "row.0 + GAP * scale"
        self.write_source(expression)
        self.assert_reported(expression, "GAP.mul_add(scale, row.0)")

    def test_float_evidence_overrides_integer_evidence(self) -> None:
        cases = (
            ("a * 3 + 1.0", "a.mul_add(3, 1.0)"),
            ("index as f32 * 3 + pad", "(index as f32).mul_add(3, pad)"),
            ("a.to_f64() * 3 + pad", "a.to_f64().mul_add(3, pad)"),
            ("a * 3 + f32::EPSILON", "a.mul_add(3, f32::EPSILON)"),
            ("rows.len() * 0.5 + pad", "rows.len().mul_add(0.5, pad)"),
            ("a * 3 + PI", "a.mul_add(3, PI)"),
        )
        for expression, rewrite in cases:
            with self.subTest(expression=expression):
                self.write_source(expression)
                self.assert_reported(expression, rewrite)

    def test_float_method_evidence_overrides_integer_evidence(self) -> None:
        for method in ("sin", "cos", "tan", "sqrt", "powi", "powf", "to_radians",
                       "to_degrees", "sin_cos", "atan2", "exp", "ln", "hypot", "recip",
                       "fract", "signum", "lerp"):
            with self.subTest(method=method):
                expression = f"a * 3 + angle.{method}()"
                self.write_source(expression)
                self.assert_reported(expression, f"a.mul_add(3, angle.{method}())")

    def test_vector_path_in_the_sum_passes_even_with_float_evidence(self) -> None:
        for expression in ("Vec3::new(x, y, z) * 0.5 + offset", "a * 0.5 + Vec3::ZERO"):
            with self.subTest(expression=expression):
                self.write_source(expression)
                self.assert_passes()

    def test_bare_vector_parameter_or_local_annotation_passes(self) -> None:
        sources = (
            "fn sample(v: Vec2) { let _ = v * 0.5 + offset; }\n",
            "fn sample() { let v: Vec2 = make(); let _ = a * 0.5 + v; }\n",
        )
        for source in sources:
            with self.subTest(source=source):
                _ = self.rs_file.write_text(source)
                self.assert_passes()

    def test_vector_constructor_binding_passes(self) -> None:
        _ = self.rs_file.write_text(
            "fn sample() { let v = Vec3::new(x, y, z); let _ = v * 0.5 + offset; }\n"
        )
        self.assert_passes()

    def test_file_vector_const_or_static_passes(self) -> None:
        for declaration in ("const", "static"):
            with self.subTest(declaration=declaration):
                _ = self.rs_file.write_text(
                    f"{declaration} K: Vec2 = Vec2::ONE;\n"
                    + "fn sample() { let _ = a * 0.5 + K; }\n"
                )
                self.assert_passes()

    def test_bare_nonfloat_annotation_passes_even_with_float_evidence(self) -> None:
        _ = self.rs_file.write_text("fn sample(i: i32) { let _ = i * 0.5 + offset; }\n")
        self.assert_passes()

    def test_unknown_call_binding_does_not_disprove_a_sum(self) -> None:
        _ = self.rs_file.write_text(
            "fn sample(values: Values) {\n    let v = values.first();\n"
            + "    let _ = v * 0.5 + offset;\n}\n"
        )
        self.assert_reported("v * 0.5 + offset", "v.mul_add(0.5, offset)", line=3)

    def test_destructured_for_binding_is_reported(self) -> None:
        _ = self.rs_file.write_text(
            "fn sample(values: Values) {\n"
            + "    for (v, _) in values { let _ = v * 0.5 + offset; }\n}\n"
        )
        self.assert_reported("v * 0.5 + offset", "v.mul_add(0.5, offset)")

    def test_unknown_field_is_not_disproved_by_its_owners_annotation(self) -> None:
        _ = self.rs_file.write_text(
            "fn sample(metrics: Metrics) {\n"
            + "    let _ = metrics.button.height * 0.5 + offset;\n}\n"
        )
        self.assert_reported("metrics.button.height * 0.5 + offset",
                             "metrics.button.height.mul_add(0.5, offset)")

    def test_for_bound_name_is_reported(self) -> None:
        _ = self.rs_file.write_text(
            "fn sample(values: Values) {\n"
            + "    for v in values { let _ = v * 0.5 + offset; }\n}\n"
        )
        self.assert_reported("v * 0.5 + offset", "v.mul_add(0.5, offset)")

    def test_if_let_bound_name_is_reported(self) -> None:
        _ = self.rs_file.write_text(
            "fn sample(value: Option<f32>) {\n"
            + "    if let Some(v) = value { let _ = v * 0.5 + offset; }\n}\n"
        )
        self.assert_reported("v * 0.5 + offset", "v.mul_add(0.5, offset)")

    def test_closure_parameter_is_reported(self) -> None:
        _ = self.rs_file.write_text(
            "fn sample(values: Values) {\n"
            + "    let _ = values.map(|v| v * 0.5 + offset);\n}\n"
        )
        self.assert_reported("v * 0.5 + offset", "v.mul_add(0.5, offset)")

    def test_match_arm_bound_name_is_reported(self) -> None:
        _ = self.rs_file.write_text(
            "fn sample(value: Option<f32>) {\n"
            + "    match value { Some(v) => { let _ = v * 0.5 + offset; }, None => {} }\n}\n"
        )
        self.assert_reported("v * 0.5 + offset", "v.mul_add(0.5, offset)")

    def test_let_else_with_a_semicolon_in_its_initializer_is_reported(self) -> None:
        _ = self.rs_file.write_text(
            "fn sample(value: Option<f32>) {\n"
            + "    let Some(v) = ({ let first = value; first }) else { return };\n"
            + "    let _ = v * 0.5 + offset;\n}\n"
        )
        self.assert_reported("v * 0.5 + offset", "v.mul_add(0.5, offset)", line=3)

    def test_let_else_bound_name_is_reported(self) -> None:
        _ = self.rs_file.write_text(
            "fn sample(value: Option<f32>) {\n"
            + "    let Some(v) = value else { return };\n"
            + "    let _ = v * 0.5 + offset;\n}\n"
        )
        self.assert_reported("v * 0.5 + offset", "v.mul_add(0.5, offset)", line=3)

    def test_both_products_of_one_sum_give_one_finding(self) -> None:
        expression = "a * 2.0 + b * 2.0"
        self.write_source(expression)
        self.assert_reported(expression, "a.mul_add(2.0, b * 2.0)")
        records = (self.state / "blocks.jsonl").read_text().split("\n")
        self.assertEqual(len([line for line in records if line]), 1)

    def test_macro_rules_definition_is_not_scanned(self) -> None:
        _ = self.rs_file.write_text(
            "macro_rules! example { () => { fn hidden() { let _ = a * 2.0 + c; } }; }\n"
            + "fn sample() {}\n"
        )
        self.assert_passes()

    def test_debug_assert_and_vec_macro_arguments_are_scanned(self) -> None:
        for invocation in ("debug_assert!(a * 0.5 + c > limit)", "vec![a * 0.5 + c]"):
            with self.subTest(invocation=invocation):
                _ = self.rs_file.write_text("fn sample() {\n    let _ = " + invocation + ";\n}\n")
                self.assert_reported("a * 0.5 + c", "a.mul_add(0.5, c)")

    def test_product_run_ending_in_division_or_remainder_is_not_reported(self) -> None:
        for expression in ("a * b / scale + pad", "a * b % scale + pad", "x += a * b / scale"):
            with self.subTest(expression=expression):
                self.write_source(expression)
                self.assert_passes()

    def test_product_run_ending_in_multiplication_is_reported(self) -> None:
        for expression, rewrite in (
            ("a / b * c + d", "(a / b).mul_add(c, d)"),
            ("a % b * c + d", "(a % b).mul_add(c, d)"),
        ):
            with self.subTest(expression=expression):
                self.write_source(expression)
                self.assert_reported(expression, rewrite)

    def test_unary_receivers_and_negated_compound_operands_are_parenthesized(self) -> None:
        cases = (
            ("-a * b + c", "(-a).mul_add(b, c)"),
            ("&a * b + c", "(&a).mul_add(b, c)"),
            ("*a * b + c", "(*a).mul_add(b, c)"),
            ("a * b - (c + d)", "a.mul_add(b, -(c + d))"),
            ("c - a * (b + d)", "a.mul_add(-(b + d), c)"),
            ("a * b - c * d", "a.mul_add(b, -(c * d))"),
        )
        for expression, rewrite in cases:
            with self.subTest(expression=expression):
                self.write_source(expression)
                self.assert_reported(expression, rewrite)

    def test_try_and_method_chain_operands_are_reported(self) -> None:
        expression = "height()?.abs() * scale + offset()"
        self.write_source(expression)
        self.assert_reported(expression, "height()?.abs().mul_add(scale, offset())")

    def test_item_and_inherited_exemptions_pass(self) -> None:
        cases = (
            "#[allow(clippy::suboptimal_flops)]\nfn sample() { let _ = 1.0_f32 * 2.0 + 3.0; }\n",
            "#[expect(clippy::nursery)]\nimpl Thing { fn sample(&self) { let _ = 1.0_f32 * 2.0 + 3.0; } }\n",
            "#[cfg_attr(test, allow(clippy::suboptimal_flops))]\n" +
            "mod tests { fn sample() { let _ = 1.0_f32 * 2.0 + 3.0; } }\n",
            "#![allow(clippy::nursery)]\nfn sample() { let _ = 1.0_f32 * 2.0 + 3.0; }\n",
        )
        for source in cases:
            with self.subTest(source=source.splitlines()[0]):
                _ = self.rs_file.write_text(source)
                self.assert_passes()

    def test_unrelated_allow_does_not_hide_a_finding(self) -> None:
        _ = self.rs_file.write_text(
            "#[allow(clippy::too_many_lines)]\n" +
            "fn sample(x: f32) { let _ = x * 2.0 + 3.0; }\n"
        )
        self.assertIn("clippy::suboptimal_flops", cast(str, self.decision()["reason"]))

    def test_const_items_and_const_functions_pass(self) -> None:
        _ = self.rs_file.write_text(
            "const VALUE: f32 = 1.0_f32 * 2.0 + 3.0;\n" +
            "const fn sample() -> f32 { 1.0_f32 * 2.0 + 3.0 }\n"
        )
        self.assert_passes()

    def test_parent_scan_preserves_nested_const_and_allowed_function_exemptions(self) -> None:
        _ = self.rs_file.write_text(
            "fn outer() {\n"
            + "    const fn constant(a: f32, c: f32) -> f32 { a * 0.5 + c }\n"
            + "    #[allow(clippy::suboptimal_flops)]\n"
            + "    fn allowed(a: f32, c: f32) -> f32 { a * 0.5 + c }\n}\n"
        )
        self.assert_passes()

    def test_enabled_nested_function_is_reported_once(self) -> None:
        _ = self.rs_file.write_text(
            "fn outer() {\n"
            + "    fn inner(a: f32, c: f32) -> f32 { a * 0.5 + c }\n}\n"
        )
        self.assert_reported("a * 0.5 + c", "a.mul_add(0.5, c)")

    def test_const_and_static_initializers_with_blocks_keep_their_exemptions(self) -> None:
        _ = self.rs_file.write_text(
            "fn sample(a: f32, c: f32) {\n"
            + "    const VALUE: f32 = { let v = 2.0; v * 0.5 + 1.0 };\n"
            + "    static OTHER: f32 = { let v = 2.0; v * 0.5 + 1.0 };\n"
            + "    let _ = a * 0.5 + c;\n}\n"
        )
        self.assert_reported("a * 0.5 + c", "a.mul_add(0.5, c)", line=4)

    def test_no_std_crate_passes(self) -> None:
        _ = self.rs_file.write_text(
            "#![no_std]\nfn sample() { let _ = 1.0_f32 * 2.0 + 3.0; }\n"
        )
        self.assert_passes()

    def test_spaced_no_std_inner_attribute_exempts_target(self) -> None:
        """Valid whitespace between #, !, and [ preserves the exemption."""
        for attribute in (
            "# ! [ no_std ]", "#! [no_std]", "# ![cfg_attr(feature = \"bare\", no_std)]",
        ):
            with self.subTest(attribute=attribute):
                _ = self.rs_file.write_text(
                    attribute + "\nfn sample(a: f32) { let _ = a * 0.5 + 1.0; }\n"
                )
                self.assert_passes()
        _ = self.rs_file.write_text(
            '// # ! [ no_std ]\nconst NOTE: &str = "# ! [ no_std ]";\n'
            + "fn sample(a: f32) { let _ = a * 0.5 + 1.0; }\n"
        )
        self.assertEqual(self.decision()["decision"], "block")

    def test_bin_main_checks_only_its_own_no_std_attribute(self) -> None:
        """A sibling bin.rs cannot exempt a directory bin's main.rs."""
        main = self.package / "src/bin/widget/main.rs"
        main.parent.mkdir(parents=True)
        _ = main.write_text("fn sample(a: f32) { let _ = a * 0.5 + 1.0; }\n")
        _ = (self.package / "src/bin/widget.rs").write_text("#![no_std]\n")
        self.assertEqual(mul_add_lib.suboptimal_flops_scope(main).state, mul_add_lib.ScopeState.ENABLED)
        result = self.run_hook(file=main)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(cast(dict[str, object], json.loads(result.stdout))["decision"], "block")

    def test_parenthesized_sqrt_receiver_passes(self) -> None:
        _ = self.rs_file.write_text(
            "fn sample(a: f32) {\n" +
            "    let _ = ((a * 0.5 + 1.0)).sqrt();\n" +
            "}\n"
        )
        self.assert_passes()

    def test_comments_and_strings_do_not_create_findings(self) -> None:
        _ = self.rs_file.write_text(
            "fn sample() {\n" +
            "    // 1.0_f32 * 2.0 + 3.0\n" +
            '    let text = "1.0_f32 * 2.0 + 3.0";\n' +
            "    let _ = 'x';\n" +
            "}\n"
        )
        self.assert_passes()

    def test_unbalanced_source_passes(self) -> None:
        _ = self.rs_file.write_text("fn sample() { let _ = 1.0_f32 * 2.0 + 3.0;\n")
        self.assert_passes()

    def test_disabled_scope_and_missing_manifest_pass(self) -> None:
        self.write_source("a * 0.5 + c")
        _ = (self.workspace / "Cargo.toml").write_text(
            '[workspace]\nmembers = ["crate"]\n' +
            '[workspace.lints.clippy]\npedantic = "deny"\n'
        )
        self.assert_passes()
        (self.package / "Cargo.toml").unlink()
        (self.workspace / "Cargo.toml").unlink()
        self.assert_passes()

    def test_explicit_lint_enables_scope_without_nursery(self) -> None:
        self.write_source("a * 0.5 + c")
        _ = (self.package / "Cargo.toml").write_text(
            '[package]\nname = "sample"\nversion = "0.1.0"\n' +
            '[lints.clippy]\nsuboptimal_flops = { level = "warn" }\n'
        )
        self.assertIn("clippy::suboptimal_flops", cast(str, self.decision()["reason"]))

    def test_explicit_lint_allow_overrides_nursery(self) -> None:
        self.write_source("a * 0.5 + c")
        _ = (self.workspace / "Cargo.toml").write_text(
            '[workspace]\nmembers = ["crate"]\n' +
            '[workspace.lints.clippy]\nnursery = "deny"\nsuboptimal_flops = "allow"\n'
        )
        self.assert_passes()

    def test_malformed_manifest_passes(self) -> None:
        self.write_source("a * 0.5 + c")
        _ = (self.package / "Cargo.toml").write_text("[package\n")
        self.assert_passes()

    def test_non_rust_path_and_unknown_payload_pass(self) -> None:
        other = self.package / "src/lib.py"
        _ = other.write_text("pass\n")
        self.assert_passes(file=other)
        self.assert_passes(payload=json.dumps({"tool_name": "Edit", "tool_input": {}}))

    def test_malformed_stdin_passes(self) -> None:
        self.assert_passes(payload="{")

    def test_one_finding_has_exact_conditional_messages_and_one_log_line(self) -> None:
        self.write_source("a * 0.5 + c")
        decision = self.decision()
        description = ("crate/src/lib.rs:2: if a * 0.5 + c is f32 or f64 arithmetic, "
                       + "write a.mul_add(0.5, c) (clippy::suboptimal_flops)")
        self.assertEqual(decision["reason"], description
                         + ". Leave integer and vector arithmetic as it is. The edit was applied.")
        self.assertEqual(decision["systemMessage"], "mul_add: lib.rs has 1 possible multiply-add expression(s)")
        self.assertEqual(decision["hookSpecificOutput"], {
            "hookEventName": "PostToolUse", "additionalContext": description,
        })
        records = (self.state / "blocks.jsonl").read_text().splitlines()
        self.assertEqual(len(records), 1)
        record = cast(dict[str, object], json.loads(records[0]))
        self.assertEqual(record["agent"], "claude")
        self.assertEqual(record["file"], str(self.rs_file))
        self.assertEqual(record["line"], 2)
        self.assertEqual(record["expression"], "a * 0.5 + c")
        self.assertIn("at", record)

    def test_no_std_is_decided_for_each_target(self) -> None:
        expression = "fn sample(a: f32) { let _ = a * 0.5 + 1.0; }\n"
        main = self.package / "src/main.rs"
        module = self.package / "src/helper.rs"
        _ = self.rs_file.write_text("#![ no_std ]\n" + expression)
        _ = main.write_text(expression)
        _ = module.write_text(expression)
        self.assert_passes(file=self.rs_file)
        self.assert_passes(file=module)
        result = self.run_hook(file=main)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(cast(dict[str, object], json.loads(result.stdout))["decision"], "block")

    def test_cfg_attr_and_attributes_in_comments_or_strings(self) -> None:
        expression = "fn sample(a: f32) { let _ = a * 0.5 + 1.0; }\n"
        _ = self.rs_file.write_text("#![cfg_attr(feature = \"bare\", no_std)]\n" + expression)
        self.assert_passes()
        _ = self.rs_file.write_text(
            '// #![no_std]\nconst NOTE: &str = "#![no_std]";\n' + expression
        )
        self.assertEqual(self.decision()["decision"], "block")

    def test_internal_error_emits_one_system_message_and_passes(self) -> None:
        self.write_source("a * 0.5 + c")
        payload = self.edit_payload("Write", {"content": self.rs_file.read_text()})
        _ = self.rs_file.write_bytes(b"\xff * +")
        result = self.run_hook(payload=payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 1)
        message = cast(dict[str, object], json.loads(result.stdout))
        self.assertEqual(set(message), {"systemMessage"})
        self.assertTrue(cast(str, message["systemMessage"]).startswith(
            "mul_add hook error: UnicodeDecodeError: "
        ))
        self.assertFalse((self.state / "blocks.jsonl").exists())

    def test_multiple_findings_join_conditional_messages_in_source_order(self) -> None:
        _ = self.rs_file.write_text(
            "fn sample(a: f32, c: f32) {\n"
            + "    let first = a * 0.5 + c;\n"
            + "    let second = a * 2.0 - c;\n}\n"
        )
        descriptions = [
            "crate/src/lib.rs:2: if a * 0.5 + c is f32 or f64 arithmetic, "
            + "write a.mul_add(0.5, c) (clippy::suboptimal_flops)",
            "crate/src/lib.rs:3: if a * 2.0 - c is f32 or f64 arithmetic, "
            + "write a.mul_add(2.0, -c) (clippy::suboptimal_flops)",
        ]
        decision = self.decision()
        self.assertEqual(decision["reason"], "; ".join(descriptions)
                         + ". Leave integer and vector arithmetic as it is. The edit was applied.")
        context = cast(dict[str, str], decision["hookSpecificOutput"])
        self.assertEqual(context["additionalContext"], "\n".join(descriptions))
        self.assertEqual(decision["systemMessage"], "mul_add: lib.rs has 2 possible multiply-add expression(s)")

    def test_write_and_multiedit_block(self) -> None:
        self.write_source("a * 0.5 + c")
        for tool in ("Write", "MultiEdit"):
            with self.subTest(tool=tool):
                self.assertEqual(self.decision(tool)["decision"], "block")

    def test_edit_writing_a_float_multiply_add_line_blocks(self) -> None:
        self.write_source("a * 0.5 + c")
        payload = self.edit_payload("Edit", {
            "old_string": "    let _ = c;\n",
            "new_string": "    let _ = a * 0.5 + c;\n",
        })
        self.assertIn("crate/src/lib.rs:2:", cast(str, self.decision(payload=payload)["reason"]))

    def test_edit_elsewhere_ignores_an_old_multiply_add_and_writes_no_log(self) -> None:
        _ = self.rs_file.write_text(
            "fn sample(a: f32, c: f32) {\n"
            + "    let old = a * 0.5 + c;\n"
            + "    let other = 2.0;\n}\n"
        )
        self.assert_passes(payload=self.edit_payload("Edit", {
            "old_string": "    let other = 1.0;",
            "new_string": "    let other = 2.0;",
        }))

    def test_edit_of_part_of_a_line_creating_a_multiply_add_blocks(self) -> None:
        self.write_source("a * 0.5 + c")
        payload = self.edit_payload("Edit", {"old_string": "0.5", "new_string": "a * 0.5"})
        self.assertIn("if a * 0.5 + c is f32 or f64 arithmetic", cast(str, self.decision(payload=payload)["reason"]))

    def test_edit_with_an_old_multiply_add_in_shared_context_passes(self) -> None:
        context = "    let old = a * 0.5 + c;\n"
        new = "    let before = 2.0;\n" + context + "    let after = 3.0;\n"
        _ = self.rs_file.write_text("fn sample(a: f32, c: f32) {\n" + new + "}\n")
        self.assert_passes(payload=self.edit_payload("Edit", {
            "old_string": "    let before = 1.0;\n" + context + "    let after = 3.0;\n",
            "new_string": new,
        }))

    def test_edit_appending_a_line_after_an_old_multiply_add_passes(self) -> None:
        old = "    let old = a * 0.5 + c;"
        new = old + "\n    let added = 2.0;"
        _ = self.rs_file.write_text("fn sample(a: f32, c: f32) {\n" + new + "\n}\n")
        result = self.run_hook(payload=self.edit_payload("Edit", {
            "old_string": old, "new_string": new,
        }))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertFalse((self.state / "blocks.jsonl").exists())

    def test_edit_inserting_a_line_before_an_old_multiply_add_passes(self) -> None:
        old = "    let old = a * 0.5 + c;"
        new = "    let added = 2.0;\n" + old
        _ = self.rs_file.write_text(
            "fn sample(a: f32, c: f32) {\n    let before = 1.0;\n" + new + "\n}\n"
        )
        result = self.run_hook(payload=self.edit_payload("Edit", {
            "old_string": old, "new_string": new,
        }))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertFalse((self.state / "blocks.jsonl").exists())

    def test_edit_appending_a_multiply_add_reports_only_the_added_line(self) -> None:
        old = "    let old = a * 0.5 + c;"
        new = old + "\n    let added = a * 2.0 + c;"
        _ = self.rs_file.write_text("fn sample(a: f32, c: f32) {\n" + new + "\n}\n")
        decision = self.decision(payload=self.edit_payload("Edit", {
            "old_string": old, "new_string": new,
        }))
        self.assertEqual(decision["reason"],
                         "crate/src/lib.rs:3: if a * 2.0 + c is f32 or f64 arithmetic, "
                         + "write a.mul_add(2.0, c) (clippy::suboptimal_flops)"
                         + ". Leave integer and vector arithmetic as it is. The edit was applied.")
        records = (self.state / "blocks.jsonl").read_text().split("\n")
        self.assertEqual([cast(dict[str, object], json.loads(line))["line"]
                          for line in records if line], [3])

    def test_edit_inserting_a_multiply_add_reports_only_the_added_line(self) -> None:
        old = "    let old = a * 0.5 + c;"
        new = "    let added = a * 2.0 + c;\n" + old
        _ = self.rs_file.write_text(
            "fn sample(a: f32, c: f32) {\n    let before = 1.0;\n" + new + "\n}\n"
        )
        decision = self.decision(payload=self.edit_payload("Edit", {
            "old_string": old, "new_string": new,
        }))
        self.assertEqual(decision["reason"],
                         "crate/src/lib.rs:3: if a * 2.0 + c is f32 or f64 arithmetic, "
                         + "write a.mul_add(2.0, c) (clippy::suboptimal_flops)"
                         + ". Leave integer and vector arithmetic as it is. The edit was applied.")
        records = (self.state / "blocks.jsonl").read_text().split("\n")
        self.assertEqual([cast(dict[str, object], json.loads(line))["line"]
                          for line in records if line], [3])

    def test_multiedit_reports_only_entries_writing_a_multiply_add(self) -> None:
        _ = self.rs_file.write_text(
            "fn sample(a: f32, c: f32) {\n"
            + "    let old = a * 0.5 + c;\n"
            + "    let other = 2.0;\n"
            + "    let added = a * 2.0 + c;\n}\n"
        )
        payload = self.edit_payload("MultiEdit", {"edits": [
            {"old_string": "    let other = 1.0;", "new_string": "    let other = 2.0;"},
            {"old_string": "    let added = c;", "new_string": "    let added = a * 2.0 + c;"},
        ]})
        reason = cast(str, self.decision(payload=payload)["reason"])
        self.assertIn("crate/src/lib.rs:4:", reason)
        self.assertNotIn("a * 0.5 + c", reason)
        records = (self.state / "blocks.jsonl").read_text().splitlines()
        self.assertEqual(len(records), 1)
        self.assertEqual(cast(dict[str, object], json.loads(records[0]))["line"], 4)

    def test_multiedit_with_no_new_multiply_add_passes(self) -> None:
        _ = self.rs_file.write_text(
            "fn sample(a: f32, c: f32) {\n"
            + "    let old = a * 0.5 + c;\n"
            + "    let first = 2.0;\n    let second = 4.0;\n}\n"
        )
        self.assert_passes(payload=self.edit_payload("MultiEdit", {"edits": [
            {"old_string": "    let first = 1.0;", "new_string": "    let first = 2.0;"},
            {"old_string": "    let second = 3.0;", "new_string": "    let second = 4.0;"},
        ]}))

    def test_payload_without_new_string_passes(self) -> None:
        self.write_source("a * 0.5 + c")
        self.assert_passes(payload=self.edit_payload("Edit", {"old_string": "c"}))

    def test_empty_edit_core_skips_detector_import(self) -> None:
        self.write_source("a * 0.5 + c")
        source = self.rs_file.read_text()
        payload = self.edit_payload("Edit", {"old_string": source, "new_string": source})
        result = subprocess.run(
            [sys.executable, "-X", "importtime", str(HOOK)], input=payload,
            capture_output=True, text=True, check=False, env=self.environment, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("mul_add_lib", result.stderr)
        self.assertFalse((self.state / "blocks.jsonl").exists())

    def test_mul_add_findings_keeps_any_written_line_of_an_expression(self) -> None:
        _ = self.rs_file.write_text(
            "fn sample(a: f32, c: f32) {\n"
            + "    let _ = a\n        * 0.5\n        + c;\n}\n"
        )
        for written_line in (2, 3, 4):
            with self.subTest(written_line=written_line):
                scope, findings = mul_add_lib.mul_add_findings(
                    self.rs_file, frozenset({written_line})
                )
                self.assertTrue(scope.enabled)
                self.assertEqual([finding.line for finding in findings], [2])
        for written in (frozenset[int](), frozenset({1, 5})):
            with self.subTest(written=written):
                _, findings = mul_add_lib.mul_add_findings(self.rs_file, written)
                self.assertEqual(findings, [])

    def test_edit_of_a_call_based_sum_reports_only_that_sum(self) -> None:
        _ = self.rs_file.write_text(
            "fn sample() {\n    let old = metrics.button.height * 0.5 + offset;\n"
            + "    let changed = bounds.height() * 0.5 + pad;\n}\n"
        )
        decision = self.decision(payload=self.edit_payload("Edit", {
            "old_string": "    let changed = pad;",
            "new_string": "    let changed = bounds.height() * 0.5 + pad;",
        }))
        description = ("crate/src/lib.rs:3: if bounds.height() * 0.5 + pad is f32 or f64 arithmetic, "
                       + "write bounds.height().mul_add(0.5, pad) (clippy::suboptimal_flops)")
        self.assertEqual(decision["reason"], description
                         + ". Leave integer and vector arithmetic as it is. The edit was applied.")
        records = (self.state / "blocks.jsonl").read_text().split("\n")
        self.assertEqual([cast(dict[str, object], json.loads(line))["line"]
                          for line in records if line], [3])

    def test_edit_elsewhere_ignores_an_old_unknown_field_sum(self) -> None:
        _ = self.rs_file.write_text(
            "fn sample() {\n    let old = metrics.button.height * 0.5 + offset;\n"
            + "    let other = 2.0;\n}\n"
        )
        self.assert_passes(payload=self.edit_payload("Edit", {
            "old_string": "    let other = 1.0;", "new_string": "    let other = 2.0;",
        }))

    def test_edit_writing_only_the_second_line_of_a_sum_reports_the_full_sum(self) -> None:
        _ = self.rs_file.write_text(
            "fn sample() {\n    let _ = value() * scale\n        + offset();\n}\n"
        )
        payload = self.edit_payload("Edit", {
            "old_string": "        + previous();", "new_string": "        + offset();",
        })
        decision = self.decision(payload=payload)
        description = ("crate/src/lib.rs:2: if value() * scale + offset() is f32 or f64 arithmetic, "
                       + "write value().mul_add(scale, offset()) (clippy::suboptimal_flops)")
        self.assertEqual(decision["reason"], description
                         + ". Leave integer and vector arithmetic as it is. The edit was applied.")
        context = cast(dict[str, str], decision["hookSpecificOutput"])
        self.assertEqual(context["additionalContext"], description)
        record = cast(dict[str, object], json.loads((self.state / "blocks.jsonl").read_text()))
        self.assertEqual(record["line"], 2)
        self.assertEqual(record["expression"], "value() * scale + offset()")

    def test_edit_writing_neither_line_of_an_old_sum_passes(self) -> None:
        _ = self.rs_file.write_text(
            "fn sample() {\n    let _ = value() * scale\n        + offset();\n"
            + "    let other = 2.0;\n}\n"
        )
        self.assert_passes(payload=self.edit_payload("Edit", {
            "old_string": "    let other = 1.0;", "new_string": "    let other = 2.0;",
        }))

    def test_settings_registers_after_fn_length(self) -> None:
        settings = cast(dict[str, object], json.loads(SETTINGS.read_text()))
        hooks = cast(dict[str, list[dict[str, object]]], settings["hooks"])
        groups = [group for group in hooks["PostToolUse"]
                  if group.get("matcher") == "Edit|MultiEdit|Write"]
        self.assertEqual(len(groups), 1)
        commands = cast(list[dict[str, object]], groups[0]["hooks"])
        self.assertEqual(
            [cast(str, handler["command"]) for handler in commands[:3]],
            [
                '"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/post-tool-use-basedpyright.py"',
                '"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/post-tool-use-fn-length.py"',
                '"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/post-tool-use-mul-add.py"',
            ],
        )

    def test_unsuffixed_literal_receiver_is_swapped_or_skipped(self) -> None:
        _ = self.rs_file.write_text(
            "fn sample(v: f32, c: f32) {\n" +
            " let _ = 2.0 * v + c;\n" +
            " let _ = c - 2.0 * v;\n" +
            " let _ = 2.0 * 3.0 + c;\n" +
            "}\n"
        )
        findings = mul_add_lib.mul_add_findings(self.rs_file)[1]
        self.assertEqual([finding.rewrite for finding in findings],
                         ["v.mul_add(2.0, c)", "v.mul_add(-2.0, c)"])

    def test_additive_chain_rewrite_keeps_full_clippy_span(self) -> None:
        _ = self.rs_file.write_text(
            "fn sample(x: f32, y: f32, a: f32, b: f32, v: f32) {\n" +
            " let _ = y - x * 0.5 + 1.0;\n" +
            " let _ = -x * 0.5 + 1.0;\n" +
            " let _ = a - b + 2.0 * v;\n" +
            "}\n"
        )
        findings = mul_add_lib.mul_add_findings(self.rs_file)[1]
        self.assertEqual([(finding.expr, finding.rewrite) for finding in findings], [
            ("y - x * 0.5", "x.mul_add(-0.5, y)"),
            ("-x * 0.5 + 1.0", "(-x).mul_add(0.5, 1.0)"),
            ("a - b + 2.0 * v", "v.mul_add(2.0, a - b)"),
        ])

    def test_unary_product_at_function_body_start_keeps_its_sign(self) -> None:
        _ = self.rs_file.write_text("fn sample(x: f32) -> f32 { -x * 0.5 + 1.0 }\n")
        findings = mul_add_lib.mul_add_findings(self.rs_file)[1]
        self.assertEqual([(finding.expr, finding.rewrite) for finding in findings],
                         [("-x * 0.5 + 1.0", "(-x).mul_add(0.5, 1.0)")])

    def test_const_blocks_and_local_static_initializers_are_skipped(self) -> None:
        _ = self.rs_file.write_text(
            "fn sample(x: f32) {\n" +
            " let _ = const { x * 0.5 + 1.0 };\n" +
            " static VALUE: f32 = 1.0_f32 * 2.0 + 3.0;\n" +
            " let _ = x * 0.5 + 1.0;\n" +
            "}\n"
        )
        self.assertEqual([finding.line for finding in mul_add_lib.mul_add_findings(self.rs_file)[1]], [4])

    def test_indexed_factors_and_addends_keep_their_full_source_text(self) -> None:
        for expression, rewrite in (
            ("x + 2.0 * b[0]", "b[0].mul_add(2.0, x)"),
            ("x - 2.0 * b[1]", "b[1].mul_add(-2.0, x)"),
            ("2.0 * x + c[0]", "x.mul_add(2.0, c[0])"),
            ("x * 2.0 - c[1]", "x.mul_add(2.0, -(c[1]))"),
        ):
            with self.subTest(expression=expression):
                self.write_source(expression)
                self.assert_reported(expression, rewrite)

    def patch(self, body: str) -> subprocess.CompletedProcess[str]:
        return self.run_hook(payload=json.dumps({
            "tool_name": "apply_patch",
            "cwd": str(self.workspace),
            "tool_input": {"command": "*** Begin Patch\n" + body + "*** End Patch\n"},
        }))

    def test_add_update_move_and_delete_headers(self) -> None:
        self.write_source("a * 0.5 + c")
        added = self.package / "src/added.rs"
        moved = self.package / "src/moved.rs"
        _ = added.write_text(self.rs_file.read_text())
        _ = moved.write_text(self.rs_file.read_text())
        result = self.patch(
            "*** Delete File: crate/src/lib.rs\n"
            + "*** Add File: crate/src/added.rs\n"
            + "".join("+" + line + "\n" for line in added.read_text().removesuffix("\n").split("\n"))
            + "*** Update File: crate/src/old.rs\n"
            + "*** Move to: crate/src/moved.rs\n@@\n"
            + "-fn sample(a: f32, c: f32, mut x: f32) { let _ = c; }\n"
            + "".join("+" + line + "\n" for line in moved.read_text().removesuffix("\n").split("\n"))
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        decision = cast(dict[str, object], json.loads(result.stdout))
        self.assertEqual(decision["decision"], "block")
        reason = cast(str, decision["reason"])
        self.assertLess(reason.index("added.rs"), reason.index("moved.rs"))
        self.assertNotIn("lib.rs", reason)
        records = [cast(dict[str, object], json.loads(line)) for line in
                   (self.state / "blocks.jsonl").read_text().splitlines()]
        self.assertEqual([record["file"] for record in records], [str(added), str(moved)])
        self.assertEqual({record["agent"] for record in records}, {"codex"})

    def test_shared_parser_carries_claude_replacements_in_payload_order(self) -> None:
        cases: tuple[tuple[str, dict[str, object], tuple[fn_length_lib.Replacement, ...]], ...] = (
            ("Edit", {"old_string": "old", "new_string": "new"}, (("old", "new"),)),
            ("MultiEdit", {"edits": [
                {"old_string": "first", "new_string": "changed first"},
                {"old_string": "second", "new_string": "changed second"},
            ]}, (("first", "changed first"), ("second", "changed second"))),
            ("Write", {"content": "all source\n"}, (("", "all source\n"),)),
        )
        for tool, inputs, replacements in cases:
            with self.subTest(tool=tool):
                edit = fn_length_lib.applied_rust_edits(self.edit_payload(tool, inputs))
                self.assertIsInstance(edit, fn_length_lib.AppliedRustEdits)
                if isinstance(edit, fn_length_lib.AppliedRustEdits):
                    self.assertEqual(edit.agent, "claude")
                    self.assertEqual(edit.tool, tool)
                    self.assertEqual(edit.files, (str(self.rs_file),))
                    self.assertEqual(edit.replacements, (replacements,))

    def test_shared_parser_omits_invalid_pairs_and_keeps_the_file(self) -> None:
        cases: tuple[tuple[str, dict[str, object]], ...] = (
            ("Edit", {"old_string": "old"}),
            ("Edit", {"new_string": "new"}),
            ("Edit", {"old_string": 1, "new_string": "new"}),
            ("Edit", {"old_string": "old", "new_string": None}),
            ("Write", {}),
            ("Write", {"content": 1}),
            ("MultiEdit", {}),
            ("MultiEdit", {"edits": [{"old_string": "old"}, {"new_string": "new"}]}),
        )
        for tool, inputs in cases:
            with self.subTest(tool=tool, inputs=inputs):
                edit = fn_length_lib.applied_rust_edits(self.edit_payload(tool, inputs))
                self.assertIsInstance(edit, fn_length_lib.AppliedRustEdits)
                if isinstance(edit, fn_length_lib.AppliedRustEdits):
                    self.assertEqual(edit.files, (str(self.rs_file),))
                    self.assertEqual(edit.replacements, ((),))

    def test_shared_parser_keeps_valid_multiedit_pairs_among_invalid_entries(self) -> None:
        payload = self.edit_payload("MultiEdit", {"edits": [
            {"old_string": "first", "new_string": "one"},
            {"old_string": "missing new"},
            None,
            {"old_string": "invalid new", "new_string": 2},
            {"old_string": "second", "new_string": "two"},
        ]})
        edit = fn_length_lib.applied_rust_edits(payload)
        self.assertIsInstance(edit, fn_length_lib.AppliedRustEdits)
        if isinstance(edit, fn_length_lib.AppliedRustEdits):
            self.assertEqual(edit.replacements, ((("first", "one"), ("second", "two")),))

    def test_shared_parser_carries_patch_chunks_context_and_empty_lines(self) -> None:
        command = (
            "*** Begin Patch\n*** Add File: crate/src/new.rs\n+first\n+\n+last\n"
            + "*** Update File: crate/src/lib.rs\n@@ first chunk\n"
            + " before\n-old\n+new\n\n after\n@@ second chunk\n-last\n+final\n"
            + "*** End of File\n*** Update File: crate/src/untouched.rs\n@@\n"
            + "*** Delete File: crate/src/gone.rs\n*** Add File: README.md\n+note\n"
            + "*** End Patch\n"
        )
        edit = fn_length_lib.applied_rust_edits(json.dumps({
            "tool_name": "apply_patch", "cwd": str(self.workspace),
            "tool_input": {"command": command},
        }))
        self.assertIsInstance(edit, fn_length_lib.AppliedRustEdits)
        if isinstance(edit, fn_length_lib.AppliedRustEdits):
            self.assertEqual(edit.agent, "codex")
            self.assertEqual(edit.files, (
                "crate/src/new.rs", "crate/src/lib.rs", "crate/src/untouched.rs",
            ))
            self.assertEqual(edit.files, fn_length_lib.applied_patch_rust_files(command))
            self.assertEqual(edit.replacements, (
                (("", "first\n\nlast"),),
                (("before\nold\n\nafter", "before\nnew\n\nafter"), ("last", "final")),
                (),
            ))

    def test_shared_parser_combines_repeated_paths_and_moves_their_pairs(self) -> None:
        command = (
            "*** Begin Patch\n*** Update File: crate/src/old.rs\n"
            + "*** Move to: crate/src/moved.rs\n@@\n-one\n+two\n"
            + "*** Update File: crate/src/moved.rs\n@@\n-three\n+four\n"
            + "*** Add File: crate/src/new.rs\n+first\n"
            + "*** Update File: crate/src/new.rs\n@@\n-first\n+second\n*** End Patch\n"
        )
        edit = fn_length_lib.applied_rust_edits(json.dumps({
            "tool_name": "apply_patch", "cwd": str(self.workspace),
            "tool_input": {"command": command},
        }))
        self.assertIsInstance(edit, fn_length_lib.AppliedRustEdits)
        if isinstance(edit, fn_length_lib.AppliedRustEdits):
            self.assertEqual(edit.files, ("crate/src/moved.rs", "crate/src/new.rs"))
            self.assertEqual(edit.files, fn_length_lib.applied_patch_rust_files(command))
            self.assertEqual(edit.replacements, (
                (("one", "two"), ("three", "four")),
                (("", "first"), ("first", "second")),
            ))

    def test_applied_rust_edits_preserves_non_lf_characters_in_patch_lines(self) -> None:
        for separator in ("\u2028", "\f", "\r"):
            old = 'fn sample(a: f32, c: f32) { let note = "old"; let _ = c; }'
            new = (f'fn sample(a: f32, c: f32) {{ let note = "before{separator}after"; '
                   + 'let _ = a * 0.5 + c; }')
            for header, body, replacements in (
                ("Add", "+" + new + "\n", (("", new),)),
                ("Update", "@@\n-" + old + "\n+" + new + "\n", ((old, new),)),
            ):
                with self.subTest(separator=separator, header=header):
                    command = ("*** Begin Patch\n*** " + header + " File: crate/src/lib.rs\n"
                               + body + "*** End Patch\n")
                    edit = fn_length_lib.applied_rust_edits(json.dumps({
                        "tool_name": "apply_patch", "cwd": str(self.workspace),
                        "tool_input": {"command": command},
                    }))
                    self.assertIsInstance(edit, fn_length_lib.AppliedRustEdits)
                    if isinstance(edit, fn_length_lib.AppliedRustEdits):
                        self.assertEqual(edit.files, ("crate/src/lib.rs",))
                        self.assertEqual(edit.replacements, (replacements,))

    def test_written_lines_empty_core_writes_nothing(self) -> None:
        source = "before\nunchanged\nafter\n"
        for replacements in ((), ((source, source),), (("unchanged\nremoved", "unchanged"),)):
            with self.subTest(replacements=replacements):
                self.assertEqual(fn_length_lib.written_lines(source, replacements), frozenset())

    def test_written_lines_deleting_part_of_a_line_writes_that_line(self) -> None:
        source = "before\nunchanged\nafter\n"
        self.assertEqual(fn_length_lib.written_lines(source, (("unchangedold", "unchanged"),)),
                         frozenset({2}))

    def test_written_lines_counts_both_occurrences_of_new_text(self) -> None:
        source = "header\n    let _ = a * 0.5 + c;\n    let _ = a * 0.5 + c;\nfooter\n"
        replacements = (("0.5", "a * 0.5"),)
        self.assertEqual(fn_length_lib.written_lines(source, replacements), frozenset({2, 3}))

    def test_written_lines_absent_new_text_uses_only_new_nonempty_line_text(self) -> None:
        source = "header\n    retained;\n\n    added;\n    added;\nfooter\n"
        replacements = (("retained;\nold;", "retained;\n\nadded;\nmissing;"),)
        self.assertEqual(fn_length_lib.written_lines(source, replacements), frozenset({4, 5}))

    def test_written_lines_fallback_counts_only_lf_in_rust_strings(self) -> None:
        for separator in ("\u2028", "\f", "\r"):
            with self.subTest(separator=separator):
                note = f'    let note = "before{separator}after";\n'
                expression = "    let _ = a * 0.5 + c;"
                source = "fn sample(a: f32, c: f32) {\n" + note + expression + "\n}\n"
                replacements = ((note + "    let _ = c;\nmissing context",
                                 note + expression + "\nmissing context"),)
                self.assertEqual(fn_length_lib.written_lines(source, replacements), frozenset({3}))

    def test_written_lines_removes_shared_prefix_and_suffix_context(self) -> None:
        source = "header\nbefore\nchanged\nafter\nfooter\n"
        replacements = (("before\nold\nafter\n", "before\nchanged\nafter\n"),)
        self.assertEqual(fn_length_lib.written_lines(source, replacements), frozenset({3}))

    def test_patch_update_writing_a_multiply_add_blocks(self) -> None:
        self.write_source("a * 0.5 + c")
        result = self.patch(
            "*** Update File: crate/src/lib.rs\n@@\n"
            + " fn sample(a: f32, c: f32, mut x: f32) {\n"
            + "-    let _ = c;\n+    let _ = a * 0.5 + c;\n }\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        decision = cast(dict[str, object], json.loads(result.stdout))
        self.assertEqual(decision["decision"], "block")
        self.assertIn("crate/src/lib.rs:2:", cast(str, decision["reason"]))

    def test_patch_update_with_an_old_multiply_add_as_context_passes(self) -> None:
        _ = self.rs_file.write_text(
            "fn sample(a: f32, c: f32) {\n"
            + "    let old = a * 0.5 + c;\n    let other = 2.0;\n}\n"
        )
        result = self.patch(
            "*** Update File: crate/src/lib.rs\n@@\n"
            + " fn sample(a: f32, c: f32) {\n"
            + "     let old = a * 0.5 + c;\n"
            + "-    let other = 1.0;\n+    let other = 2.0;\n }\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertFalse((self.state / "blocks.jsonl").exists())

    def test_patch_adding_a_line_before_an_old_multiply_add_context_passes(self) -> None:
        _ = self.rs_file.write_text(
            "fn sample(a: f32, c: f32) {\n    let before = 1.0;\n"
            + "    let added = 2.0;\n    let old = a * 0.5 + c;\n}\n"
        )
        result = self.patch(
            "*** Update File: crate/src/lib.rs\n@@\n"
            + "     let before = 1.0;\n+    let added = 2.0;\n"
            + "     let old = a * 0.5 + c;\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertFalse((self.state / "blocks.jsonl").exists())

    def test_patch_adding_a_multiply_add_reports_only_the_added_line(self) -> None:
        _ = self.rs_file.write_text(
            "fn sample(a: f32, c: f32) {\n    let before = 1.0;\n"
            + "    let added = a * 2.0 + c;\n    let old = a * 0.5 + c;\n}\n"
        )
        result = self.patch(
            "*** Update File: crate/src/lib.rs\n@@\n"
            + "     let before = 1.0;\n+    let added = a * 2.0 + c;\n"
            + "     let old = a * 0.5 + c;\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        decision = cast(dict[str, object], json.loads(result.stdout))
        self.assertEqual(decision["decision"], "block")
        self.assertEqual(decision["reason"],
                         "crate/src/lib.rs:3: if a * 2.0 + c is f32 or f64 arithmetic, "
                         + "write a.mul_add(2.0, c) (clippy::suboptimal_flops)"
                         + ". Leave integer and vector arithmetic as it is. The edit was applied.")
        records = (self.state / "blocks.jsonl").read_text().split("\n")
        self.assertEqual([cast(dict[str, object], json.loads(line))["line"]
                          for line in records if line], [3])

    def test_patch_add_with_a_unicode_separator_reports_the_expression_line(self) -> None:
        source = ('fn sample(a: f32, c: f32) {\n    let note = "before\u2028after";\n'
                  + "    let _ = a * 0.5 + c;\n}\n")
        _ = self.rs_file.write_text(source)
        result = self.patch(
            "*** Add File: crate/src/lib.rs\n"
            + "".join("+" + line + "\n" for line in source.removesuffix("\n").split("\n"))
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        decision = cast(dict[str, object], json.loads(result.stdout))
        self.assertEqual(decision["decision"], "block")
        self.assertIn("crate/src/lib.rs:3:", cast(str, decision["reason"]))
        records = (self.state / "blocks.jsonl").read_text().split("\n")
        self.assertEqual([cast(dict[str, object], json.loads(line))["line"]
                          for line in records if line], [3])

    def test_patch_with_crlf_line_ends_reports_the_added_line(self) -> None:
        self.write_source("a * 0.5 + c")
        lines = self.rs_file.read_text().removesuffix("\n").split("\n")
        result = self.run_hook(payload=json.dumps({
            "tool_name": "apply_patch",
            "cwd": str(self.workspace),
            "tool_input": {"command": (
                "*** Begin Patch\r\n*** Add File: crate/src/lib.rs\r\n"
                + "".join("+" + line + "\r\n" for line in lines)
                + "*** End Patch\r\n"
            )},
        }))
        self.assertEqual(result.returncode, 0, result.stderr)
        decision = cast(dict[str, object], json.loads(result.stdout))
        self.assertEqual(decision["decision"], "block")
        self.assertIn("crate/src/lib.rs:2:", cast(str, decision["reason"]))

    def test_patch_add_writing_a_multiply_add_blocks(self) -> None:
        self.write_source("a * 0.5 + c")
        result = self.patch(
            "*** Add File: crate/src/lib.rs\n"
            + "".join("+" + line + "\n" for line in self.rs_file.read_text().removesuffix("\n").split("\n"))
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        decision = cast(dict[str, object], json.loads(result.stdout))
        self.assertEqual(decision["decision"], "block")
        self.assertIn("crate/src/lib.rs:2:", cast(str, decision["reason"]))

    def test_patch_move_carries_written_lines_to_the_destination(self) -> None:
        _ = self.rs_file.write_text("mod moved;\n")
        moved = self.package / "src/moved.rs"
        _ = moved.write_text("fn sample(a: f32, c: f32) { let _ = a * 0.5 + c; }\n")
        result = self.patch(
            "*** Update File: crate/src/old.rs\n*** Move to: crate/src/moved.rs\n@@\n"
            + "-fn sample(a: f32, c: f32) { let _ = c; }\n"
            + "+fn sample(a: f32, c: f32) { let _ = a * 0.5 + c; }\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        decision = cast(dict[str, object], json.loads(result.stdout))
        self.assertEqual(decision["decision"], "block")
        self.assertIn("crate/src/moved.rs:1:", cast(str, decision["reason"]))
        records = (self.state / "blocks.jsonl").read_text().splitlines()
        self.assertEqual(len(records), 1)
        self.assertEqual(cast(dict[str, object], json.loads(records[0]))["file"], str(moved))

    def test_two_files_have_exact_conditional_messages_and_two_finding_records(self) -> None:
        self.write_source("a * 0.5 + c")
        other = self.package / "src/other.rs"
        _ = other.write_text("fn other(x: f32) { let _ = x * 2.0 + 1.0; }\n")
        result = self.patch(
            "*** Update File: crate/src/lib.rs\n@@\n"
            + "".join("+" + line + "\n" for line in self.rs_file.read_text().removesuffix("\n").split("\n"))
            + "*** Update File: crate/src/other.rs\n@@\n"
            + "".join("+" + line + "\n" for line in other.read_text().removesuffix("\n").split("\n"))
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 1)
        decision = cast(dict[str, object], json.loads(result.stdout))
        self.assertEqual(decision["decision"], "block")
        self.assertEqual(decision["continue"], True)
        descriptions = [
            "crate/src/lib.rs:2: if a * 0.5 + c is f32 or f64 arithmetic, "
            + "write a.mul_add(0.5, c) (clippy::suboptimal_flops)",
            "crate/src/other.rs:1: if x * 2.0 + 1.0 is f32 or f64 arithmetic, "
            + "write x.mul_add(2.0, 1.0) (clippy::suboptimal_flops)",
        ]
        self.assertEqual(decision["reason"], "; ".join(descriptions)
                         + ". Leave integer and vector arithmetic as it is. The edit was applied.")
        self.assertEqual(decision["systemMessage"],
                         "mul_add: 2 possible multiply-add expression(s) in "
                         + "crate/src/lib.rs, crate/src/other.rs")
        self.assertEqual(decision["hookSpecificOutput"], {
            "hookEventName": "PostToolUse", "additionalContext": "\n".join(descriptions),
        })
        records = [cast(dict[str, object], json.loads(line)) for line in
                   (self.state / "blocks.jsonl").read_text().splitlines()]
        self.assertEqual(len(records), 2)
        self.assertEqual([record["file"] for record in records], [str(self.rs_file), str(other)])
        self.assertEqual({record["agent"] for record in records}, {"codex"})
        self.assertEqual([record["line"] for record in records], [2, 1])
        self.assertEqual([record["expression"] for record in records],
                         ["a * 0.5 + c", "x * 2.0 + 1.0"])

    def test_later_scan_error_leaves_no_block_record(self) -> None:
        """A failed multi-file scan cannot leave records for an undelivered block."""
        self.write_source("a * 0.5 + c")
        other = self.package / "src/other.rs"
        _ = other.write_text("fn other(x: f32) { let _ = x * 2.0 + 1.0; }\n")
        payload = json.dumps({
            "tool_name": "apply_patch", "cwd": str(self.workspace),
            "tool_input": {"command": "*** Begin Patch\n*** Update File: crate/src/lib.rs\n@@\n"
                           + "".join("+" + line + "\n" for line in self.rs_file.read_text().removesuffix("\n").split("\n"))
                           + "*** Update File: crate/src/other.rs\n@@\n"
                           + "".join("+" + line + "\n" for line in other.read_text().removesuffix("\n").split("\n"))
                           + "*** End Patch\n"},
        })
        output = io.StringIO()
        main = cast(Callable[[], None], runpy.run_path(str(HOOK))["main"])
        first = mul_add_lib.mul_add_findings(self.rs_file)
        with (patch.dict(os.environ, self.environment),
              patch.object(sys, "stdin", io.StringIO(payload)), redirect_stdout(output),
              patch.object(mul_add_lib, "mul_add_findings",
                           side_effect=[first, RuntimeError("later file")])):
            main()
        message = cast(dict[str, object], json.loads(output.getvalue()))
        self.assertEqual(message, {"systemMessage": "mul_add hook error: RuntimeError: later file"})
        self.assertFalse((self.state / "blocks.jsonl").exists())


if __name__ == "__main__":
    _ = unittest.main()
