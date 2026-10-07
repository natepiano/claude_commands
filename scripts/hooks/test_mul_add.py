"""The mul_add hook checks only provable Clippy findings in temporary crates."""

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
        self, tool: str = "Edit", *, file: Path | None = None, payload: str | None = None
    ) -> subprocess.CompletedProcess[str]:
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

    def decision(self, tool: str = "Edit") -> dict[str, object]:
        result = self.run_hook(tool)
        self.assertEqual(result.returncode, 0, result.stderr)
        decision = cast(dict[str, object], json.loads(result.stdout))
        self.assertEqual(decision["decision"], "block")
        self.assertEqual(decision["continue"], True)
        return decision

    def assert_passes(self, *, file: Path | None = None, payload: str | None = None) -> None:
        result = self.run_hook(file=file, payload=payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        if result.stdout:
            output = cast(dict[str, object], json.loads(result.stdout))
            self.assertNotEqual(output.get("decision"), "block")
            self.assertEqual(len(result.stdout.splitlines()), 1)
        self.assertFalse((self.state / "blocks.jsonl").exists())

    def test_each_clippy_shape_with_a_float_literal_blocks(self) -> None:
        cases = (
            ("a * 0.5 + c", "a.mul_add(0.5, c)"),
            ("c + a * 0.5", "a.mul_add(0.5, c)"),
            ("a * 0.5 - c", "a.mul_add(0.5, -c)"),
            ("c - a * 0.5", "(-a).mul_add(0.5, c)"),
            ("x += a * 0.5", "mul_add(0.5, x)"),
        )
        for expression, rewrite in cases:
            with self.subTest(expression=expression):
                self.write_source(expression)
                decision = self.decision()
                reason = cast(str, decision["reason"])
                self.assertIn(f"for {expression} (clippy::suboptimal_flops)", reason)
                self.assertIn(rewrite, reason)

        self.write_source("x -= a * 0.5")
        reason = cast(str, self.decision()["reason"])
        self.assertIn("for x -= a * 0.5 (clippy::suboptimal_flops)", reason)
        self.assertTrue(
            "(-a).mul_add(0.5, x)" in reason or "a.mul_add(-0.5, x)" in reason,
            reason,
        )

    def test_nontrivial_receiver_is_parenthesized(self) -> None:
        self.write_source("(a + c) * 0.5 + c")
        self.assertIn("(a + c).mul_add(0.5, c)", cast(str, self.decision()["reason"]))

    def test_literal_spellings_are_float_proof(self) -> None:
        for literal in ("1.0", "0.5_f32", "2f64", "1e-5"):
            with self.subTest(literal=literal):
                float_type = "f64" if literal == "2f64" else "f32"
                _ = self.rs_file.write_text(
                    f"fn sample(a: {float_type}, c: {float_type}) {{\n" +
                    f"    let _ = a * {literal} + c;\n" +
                    "}\n"
                )
                self.assertIn("clippy::suboptimal_flops", cast(str, self.decision()["reason"]))

    def test_literal_proves_product_without_typed_operands(self) -> None:
        _ = self.rs_file.write_text(
            "struct Text { y: f32, height: f32 }\n" +
            "fn sample(level: f32, low: f32, gap: f32, line_height: f32, text: Text) {\n" +
            "    let _ = (level - low) * 5.0 + 0.5;\n" +
            "    let _ = text.y + text.height * 0.5;\n" +
            "    let _ = 2.0 * line_height + gap;\n" +
            "}\n"
        )
        _, findings = mul_add_lib.float_mul_add_findings(self.rs_file)
        self.assertEqual(
            [finding.expr for finding in findings],
            ["(level - low) * 5.0 + 0.5", "text.y + text.height * 0.5", "2.0 * line_height + gap"],
        )

    def test_many_findings_scan_scales_with_file_length(self) -> None:
        timings: list[float] = []
        for lines in (200, 800):
            _ = self.rs_file.write_text(
                "fn sample(a: f32, c: f32) {\n" +
                "    let _ = a * 0.5 + c;\n" * lines +
                "}\n"
            )
            started = time.perf_counter()
            _, findings = mul_add_lib.float_mul_add_findings(self.rs_file)
            timings.append(time.perf_counter() - started)
            self.assertEqual(len(findings), lines)
            self.assertEqual(findings[-1].line, lines + 1)
        self.assertLess(timings[1], timings[0] * 7)

    def test_source_without_product_skips_detector_import(self) -> None:
        _ = self.rs_file.write_text("fn sample() {\n" + "    let _ = 1.0;\n" * 248 + "}\n")
        payload = json.dumps({
            "tool_name": "Edit", "tool_input": {"file_path": str(self.rs_file)},
            "cwd": str(self.workspace),
        })
        result = subprocess.run(
            [sys.executable, "-X", "importtime", str(HOOK)], input=payload,
            capture_output=True, text=True, check=False, env=self.environment, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("mul_add_lib", result.stderr)

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
        scope, findings = mul_add_lib.float_mul_add_findings(self.rs_file)
        self.assertTrue(scope.enabled)
        self.assertEqual(len(findings), 1)
        finding = findings[0]
        self.assertEqual(
            (finding.line, finding.a, finding.b, finding.c,
             finding.expr, finding.rewrite),
            (2, "a", "0.5", "c", "a * 0.5 + c", "a.mul_add(0.5, c)"),
        )

    def test_typed_binding_or_cast_proves_float_product(self) -> None:
        sources = (
            "fn sample(b: f32, c: f32) { let a: f32 = b; let _ = a * b + c; }\n",
            "fn sample(a: f32, b: f32, c: f32) { let _ = (a as f32) * b + c; }\n",
        )
        for source in sources:
            with self.subTest(source=source):
                _ = self.rs_file.write_text(source)
                scope, findings = mul_add_lib.float_mul_add_findings(self.rs_file)
                self.assertTrue(scope.enabled)
                self.assertEqual(len(findings), 1)

    def test_if_else_initializer_is_a_plain_binding(self) -> None:
        _ = self.rs_file.write_text(
            "fn f(c: bool) -> f32 {\n" +
            "    let w = if c { 1.0 } else { 2.0 };\n" +
            "    w * 0.5 + 1.0\n" +
            "}\n"
        )
        _, findings = mul_add_lib.float_mul_add_findings(self.rs_file)
        self.assertEqual([finding.rewrite for finding in findings], ["w.mul_add(0.5, 1.0)"])

    def test_else_if_initializer_is_a_plain_binding(self) -> None:
        _ = self.rs_file.write_text(
            "fn f(c: bool, d: bool, a: f32, b: f32, e: f32) -> f32 {\n" +
            "    let w = if c { a } else if d { b } else { e };\n" +
            "    w * 0.5 + 1.0\n" +
            "}\n"
        )
        _, findings = mul_add_lib.float_mul_add_findings(self.rs_file)
        self.assertEqual([finding.rewrite for finding in findings], ["w.mul_add(0.5, 1.0)"])

    def test_if_else_initializer_does_not_make_tuple_pattern_let_else(self) -> None:
        _ = self.rs_file.write_text(
            "fn f(c: bool) -> f32 {\n" +
            "    let (w, _) = if c { (1.0, 0) } else { (2.0, 0) };\n" +
            "    w * 0.5 + 1.0\n" +
            "}\n"
        )
        _, findings = mul_add_lib.float_mul_add_findings(self.rs_file)
        self.assertEqual([finding.rewrite for finding in findings], ["w.mul_add(0.5, 1.0)"])

    def test_let_else_pattern_binding_stays_excluded(self) -> None:
        _ = self.rs_file.write_text(
            "fn f(opt: Option<f32>) -> f32 {\n" +
            "    let Some(v) = opt else { return 0.0 };\n" +
            "    v * 0.5 + 1.0\n" +
            "}\n"
        )
        _, findings = mul_add_lib.float_mul_add_findings(self.rs_file)
        self.assertEqual(findings, [])

    def test_nested_if_else_does_not_hide_outer_let_else(self) -> None:
        _ = self.rs_file.write_text(
            "fn choose(value: i32) -> Option<f32> { Some(value as f32) }\n" +
            "fn f(c: bool) -> f32 {\n" +
            "    let Some(v) = choose(if c { 1 } else { 2 }) else { return 0.0 };\n" +
            "    v * 0.5 + 1.0\n" +
            "}\n"
        )
        _, findings = mul_add_lib.float_mul_add_findings(self.rs_file)
        self.assertEqual(findings, [])

    def test_if_else_vector_initializer_stays_excluded(self) -> None:
        _ = self.rs_file.write_text(
            "fn f(c: bool) -> Vec2 {\n" +
            "    let w = if c { Vec2::ONE } else { Vec2::ZERO };\n" +
            "    w * 0.5 + 1.0\n" +
            "}\n"
        )
        _, findings = mul_add_lib.float_mul_add_findings(self.rs_file)
        self.assertEqual(findings, [])

    def test_ambiguous_field_name_does_not_prove_integer_product(self) -> None:
        _ = self.rs_file.write_text(
            "struct F { n: f32 }\n" +
            "struct I { n: i32 }\n" +
            "fn f(i: I) -> i32 { i.n * i.n + 1 }\n"
        )
        _, findings = mul_add_lib.float_mul_add_findings(self.rs_file)
        self.assertEqual(findings, [])

    def test_ambiguous_field_name_with_float_literal_still_rewrites(self) -> None:
        _ = self.rs_file.write_text(
            "struct F { n: f32 }\n" +
            "struct I { n: i32 }\n" +
            "fn f(f: F, c: f32) -> f32 { f.n * 0.5 + c }\n"
        )
        _, findings = mul_add_lib.float_mul_add_findings(self.rs_file)
        self.assertEqual([finding.rewrite for finding in findings], ["f.n.mul_add(0.5, c)"])

    def test_file_float_const_proves_product(self) -> None:
        _ = self.rs_file.write_text(
            "const K: f32 = 2.0;\n" +
            "fn f(x: f32, c: f32) -> f32 { c + x * K }\n"
        )
        _, findings = mul_add_lib.float_mul_add_findings(self.rs_file)
        self.assertEqual([finding.rewrite for finding in findings], ["x.mul_add(K, c)"])

    def test_function_local_float_const_proves_only_its_function(self) -> None:
        _ = self.rs_file.write_text(
            "fn f(x: f32, c: f32) -> f32 { const K: f32 = 2.0; c + x * K }\n" +
            "fn g(x: f32) -> f32 { x * K + x }\n"
        )
        _, findings = mul_add_lib.float_mul_add_findings(self.rs_file)
        self.assertEqual([finding.rewrite for finding in findings], ["x.mul_add(K, c)"])

    def test_sibling_function_float_const_does_not_prove_product(self) -> None:
        _ = self.rs_file.write_text(
            "fn g() -> f32 { const K: f32 = 1.0; K }\n" +
            "fn f(x: f32) -> f32 { x * K + x }\n"
        )
        _, findings = mul_add_lib.float_mul_add_findings(self.rs_file)
        self.assertEqual(findings, [])

    def test_nested_nonfloat_const_does_not_hide_file_float_const(self) -> None:
        _ = self.rs_file.write_text(
            "const K: f32 = 2.0;\n" +
            "fn g() -> u32 { const K: u32 = 1; K }\n" +
            "fn f(x: f32, c: f32) -> f32 { c + x * K }\n"
        )
        _, findings = mul_add_lib.float_mul_add_findings(self.rs_file)
        self.assertEqual([finding.rewrite for finding in findings], ["x.mul_add(K, c)"])

    def test_nonfloat_module_const_before_float_names_no_scalar(self) -> None:
        _ = self.rs_file.write_text(
            "mod b { pub const N: u32 = 1; pub fn f() -> u32 { N * N + 1 } }\n" +
            "mod a { pub const N: f32 = 1.0; }\n"
        )
        _, findings = mul_add_lib.float_mul_add_findings(self.rs_file)
        self.assertEqual(findings, [])

    def test_nonfloat_module_const_after_float_names_no_scalar(self) -> None:
        _ = self.rs_file.write_text(
            "mod a { pub const N: f32 = 1.0; }\n" +
            "mod b { pub const N: u32 = 1; pub fn f() -> u32 { N * N + 1 } }\n"
        )
        _, findings = mul_add_lib.float_mul_add_findings(self.rs_file)
        self.assertEqual(findings, [])

    def test_duplicate_float_module_consts_still_prove_product(self) -> None:
        _ = self.rs_file.write_text(
            "mod a { pub const N: f32 = 1.0; }\n" +
            "mod b { pub const N: f32 = 2.0; }\n" +
            "fn f(x: f32, c: f32) -> f32 { c + x * N }\n"
        )
        _, findings = mul_add_lib.float_mul_add_findings(self.rs_file)
        self.assertEqual([finding.rewrite for finding in findings], ["x.mul_add(N, c)"])

    def test_plain_local_shadows_file_float_const(self) -> None:
        _ = self.rs_file.write_text(
            "const K: f32 = 2.0;\n" +
            "fn f() -> i32 { let K = 2; K * K + 1 }\n"
        )
        _, findings = mul_add_lib.float_mul_add_findings(self.rs_file)
        self.assertEqual(findings, [])

    def test_plain_local_shadows_module_float_const(self) -> None:
        _ = self.rs_file.write_text(
            "mod m { pub const K: f32 = 2.0; }\n" +
            "fn f() -> i32 { let K = 2; K * K + 1 }\n"
        )
        _, findings = mul_add_lib.float_mul_add_findings(self.rs_file)
        self.assertEqual(findings, [])

    def test_file_vector_const_does_not_prove_product(self) -> None:
        _ = self.rs_file.write_text(
            "const K: Vec2 = Vec2::ONE;\n" +
            "fn f(x: f32, c: Vec2) -> Vec2 { c + x * K }\n"
        )
        _, findings = mul_add_lib.float_mul_add_findings(self.rs_file)
        self.assertEqual(findings, [])

    def test_file_float_static_proves_product(self) -> None:
        _ = self.rs_file.write_text(
            "static K: f64 = 2.0;\n" +
            "fn f(x: f64, c: f64) -> f64 { c + x * K }\n"
        )
        _, findings = mul_add_lib.float_mul_add_findings(self.rs_file)
        self.assertEqual([finding.rewrite for finding in findings], ["x.mul_add(K, c)"])

    def test_local_annotation_overrides_file_float_const(self) -> None:
        _ = self.rs_file.write_text(
            "const K: f32 = 2.0;\n" +
            "fn f(x: f32, c: Vec2) -> Vec2 {\n" +
            "    let K: Vec2 = Vec2::ONE;\n" +
            "    c + x * K\n" +
            "}\n"
        )
        _, findings = mul_add_lib.float_mul_add_findings(self.rs_file)
        self.assertEqual(findings, [])

    def test_last_float_field_in_chain_proves_literal_product(self) -> None:
        _ = self.rs_file.write_text(
            "struct S { h: f32 }\n" +
            "fn f(s: Outer, c: f32) -> f32 { c - s.inner.h * 0.5 }\n"
        )
        _, findings = mul_add_lib.float_mul_add_findings(self.rs_file)
        self.assertEqual([finding.rewrite for finding in findings], ["(-s.inner.h).mul_add(0.5, c)"])

    def test_float_fields_prove_product_without_literal(self) -> None:
        _ = self.rs_file.write_text(
            "struct S { a: f32, b: f32 }\n" +
            "fn f(s: S, c: f32) -> f32 { s.a * s.b + c }\n"
        )
        _, findings = mul_add_lib.float_mul_add_findings(self.rs_file)
        self.assertEqual([finding.rewrite for finding in findings], ["s.a.mul_add(s.b, c)"])

    def test_last_float_field_in_addend_is_accepted(self) -> None:
        _ = self.rs_file.write_text(
            "struct S { h: f32 }\n" +
            "fn f(s: Outer, x: f32) -> f32 { x * 0.5 + s.inner.h }\n"
        )
        _, findings = mul_add_lib.float_mul_add_findings(self.rs_file)
        self.assertEqual([finding.rewrite for finding in findings], ["x.mul_add(0.5, s.inner.h)"])

    def test_vector_field_does_not_prove_literal_product(self) -> None:
        _ = self.rs_file.write_text(
            "struct S { v: Vec2 }\n" +
            "fn f(s: S, c: Vec2) -> Vec2 { s.v * 0.5 + c }\n"
        )
        _, findings = mul_add_lib.float_mul_add_findings(self.rs_file)
        self.assertEqual(findings, [])

    def test_integer_product_does_not_block(self) -> None:
        _ = self.rs_file.write_text("fn sample(i: i32, j: i32) { let _ = i * 2 + j; }\n")
        self.assert_passes()

    def test_glam_vector_times_literal_does_not_block(self) -> None:
        _ = self.rs_file.write_text(
            "fn sample(w: Vec3) {\n" +
            "    let v: Vec3 = Vec3::ZERO;\n" +
            "    let _ = v * 0.5 + w;\n" +
            "}\n"
        )
        self.assert_passes()

    def test_float_parameter_does_not_prove_same_named_vector_field(self) -> None:
        _ = self.rs_file.write_text(
            "struct Thing { x: glam::Vec3 }\n" +
            "fn sample(x: f32, thing: Thing, w: glam::Vec3) {\n" +
            " let _ = thing.x * 0.5 + w;\n" +
            " let _ = x * 0.5 + 1.0;\n" +
            "}\n"
        )
        self.assertEqual([finding.line for finding in mul_add_lib.float_mul_add_findings(self.rs_file)[1]], [4])

    def test_item_and_inherited_exemptions_pass(self) -> None:
        cases = (
            "#[allow(clippy::suboptimal_flops)]\nfn sample() { let _ = 1.0 * 2.0 + 3.0; }\n",
            "#[expect(clippy::nursery)]\nimpl Thing { fn sample(&self) { let _ = 1.0 * 2.0 + 3.0; } }\n",
            "#[cfg_attr(test, allow(clippy::suboptimal_flops))]\n" +
            "mod tests { fn sample() { let _ = 1.0 * 2.0 + 3.0; } }\n",
            "#![allow(clippy::nursery)]\nfn sample() { let _ = 1.0 * 2.0 + 3.0; }\n",
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
            "const VALUE: f32 = 1.0 * 2.0 + 3.0;\n" +
            "const fn sample() -> f32 { 1.0 * 2.0 + 3.0 }\n"
        )
        self.assert_passes()

    def test_no_std_crate_passes(self) -> None:
        _ = self.rs_file.write_text(
            "#![no_std]\nfn sample() { let _ = 1.0 * 2.0 + 3.0; }\n"
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
            "    // 1.0 * 2.0 + 3.0\n" +
            '    let text = "1.0 * 2.0 + 3.0";\n' +
            "    let _ = 'x';\n" +
            "}\n"
        )
        self.assert_passes()

    def test_unbalanced_source_passes(self) -> None:
        _ = self.rs_file.write_text("fn sample() { let _ = 1.0 * 2.0 + 3.0;\n")
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

    def test_exact_reason_and_one_log_line(self) -> None:
        self.write_source("a * 0.5 + c")
        decision = self.decision()
        self.assertEqual(
            decision["reason"],
            "crate/src/lib.rs:2: write a.mul_add(0.5, c) for a * 0.5 + c " +
            "(clippy::suboptimal_flops) The edit was applied.",
        )
        records = (self.state / "blocks.jsonl").read_text().splitlines()
        self.assertEqual(len(records), 1)
        record = cast(dict[str, object], json.loads(records[0]))
        self.assertEqual(record["agent"], "claude")
        self.assertIn(str(self.rs_file), records[0])
        self.assertIn('"line": 2', records[0])
        self.assertIn('"expression": "a * 0.5 + c"', records[0])
        self.assertIn("at", record)
        context = cast(dict[str, str], decision["hookSpecificOutput"])
        self.assertEqual(
            context["additionalContext"],
            cast(str, decision["reason"]).removesuffix(" The edit was applied."),
        )

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
        _ = self.rs_file.write_bytes(b"\xff * +")
        result = self.run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 1)
        message = cast(dict[str, object], json.loads(result.stdout))
        self.assertEqual(set(message), {"systemMessage"})
        self.assertTrue(cast(str, message["systemMessage"]).startswith(
            "mul_add hook error: UnicodeDecodeError: "
        ))
        self.assertFalse((self.state / "blocks.jsonl").exists())

    def test_multiple_findings_join_reasons_in_source_order(self) -> None:
        _ = self.rs_file.write_text(
            "fn sample(a: f32, c: f32) {\n" +
            "    let first = a * 0.5 + c;\n" +
            "    let second = a * 2.0 - c;\n" +
            "}\n"
        )
        self.assertEqual(
            self.decision()["reason"],
            "crate/src/lib.rs:2: write a.mul_add(0.5, c) for a * 0.5 + c " +
            "(clippy::suboptimal_flops); crate/src/lib.rs:3: write " +
            "a.mul_add(2.0, -c) for a * 2.0 - c " +
            "(clippy::suboptimal_flops) The edit was applied.",
        )

    def test_write_and_multiedit_block(self) -> None:
        self.write_source("a * 0.5 + c")
        for tool in ("Write", "MultiEdit"):
            with self.subTest(tool=tool):
                self.assertEqual(self.decision(tool)["decision"], "block")

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

    def test_nonfloat_typed_and_pattern_bindings_are_excluded(self) -> None:
        cases = (
            "fn sample(x: &f32, c: f32) { let _ = x * 0.5 + c; }\n",
            "fn sample(v: glam::Vec3, w: glam::Vec3) { let _ = v * 0.5 + w; }\n",
            "fn sample(values: &[f32]) { let _ = values.iter().map(|v| v * 2.0 + 1.0); }\n",
            "fn sample(values: &[f32]) { for v in values { let _ = v * 0.5 + 1.0; } }\n",
            "fn sample(values: &[(&f32, f32)]) { for (v, _) in values { let _ = v * 0.5 + 1.0; } }\n",
            "fn sample(value: Option<f32>) { if let Some(v) = value { let _ = v * 0.5 + 1.0; } }\n",
            "fn sample(value: Option<&f32>) { let Some(v) = value else { return }; let _ = v * 0.5 + 1.0; }\n",
            "fn sample(value: Option<&f32>) { match value { Some(v) => { let _ = v * 0.5 + 1.0; }, None => {} } }\n",
            "fn sample(values: &[f32]) { let v = values.first(); let _ = v * 0.5 + 1.0; }\n",
        )
        for source in cases:
            with self.subTest(source=source):
                _ = self.rs_file.write_text(source)
                self.assertEqual(mul_add_lib.float_mul_add_findings(self.rs_file)[1], [])

    def test_nonfloat_file_constant_is_excluded(self) -> None:
        _ = self.rs_file.write_text(
            "const VECTOR: Vec3 = Vec3::ZERO;\n" +
            "fn sample(w: Vec3) { let _ = VECTOR * 0.5 + w; }\n"
        )
        self.assertEqual(mul_add_lib.float_mul_add_findings(self.rs_file)[1], [])

    def test_unsuffixed_literal_receiver_is_swapped_or_skipped(self) -> None:
        _ = self.rs_file.write_text(
            "fn sample(v: f32, c: f32) {\n" +
            " let _ = 2.0 * v + c;\n" +
            " let _ = c - 2.0 * v;\n" +
            " let _ = 2.0 * 3.0 + c;\n" +
            "}\n"
        )
        findings = mul_add_lib.float_mul_add_findings(self.rs_file)[1]
        self.assertEqual([finding.rewrite for finding in findings],
                         ["v.mul_add(2.0, c)", "(-v).mul_add(2.0, c)"])

    def test_additive_chain_rewrite_keeps_full_clippy_span(self) -> None:
        _ = self.rs_file.write_text(
            "fn sample(x: f32, y: f32, a: f32, b: f32, v: f32) {\n" +
            " let _ = y - x * 0.5 + 1.0;\n" +
            " let _ = -x * 0.5 + 1.0;\n" +
            " let _ = a - b + 2.0 * v;\n" +
            "}\n"
        )
        findings = mul_add_lib.float_mul_add_findings(self.rs_file)[1]
        self.assertEqual([(finding.expr, finding.rewrite) for finding in findings], [
            ("y - x * 0.5", "(-x).mul_add(0.5, y)"),
            ("-x * 0.5 + 1.0", "(-x).mul_add(0.5, 1.0)"),
            ("a - b + 2.0 * v", "v.mul_add(2.0, a - b)"),
        ])

    def test_unary_product_at_function_body_start_keeps_its_sign(self) -> None:
        _ = self.rs_file.write_text("fn sample(x: f32) -> f32 { -x * 0.5 + 1.0 }\n")
        findings = mul_add_lib.float_mul_add_findings(self.rs_file)[1]
        self.assertEqual([(finding.expr, finding.rewrite) for finding in findings],
                         [("-x * 0.5 + 1.0", "(-x).mul_add(0.5, 1.0)")])

    def test_const_blocks_and_local_static_initializers_are_skipped(self) -> None:
        _ = self.rs_file.write_text(
            "fn sample(x: f32) {\n" +
            " let _ = const { x * 0.5 + 1.0 };\n" +
            " static VALUE: f32 = 1.0 * 2.0 + 3.0;\n" +
            " let _ = x * 0.5 + 1.0;\n" +
            "}\n"
        )
        self.assertEqual([finding.line for finding in mul_add_lib.float_mul_add_findings(self.rs_file)[1]], [4])


    def test_indexed_operand_is_never_cut_from_its_index(self) -> None:
        _ = self.rs_file.write_text(
            "fn sample(x: f32, b: [f32; 3], c: [f32; 3]) {\n" +
            " let _ = x + 2.0 * b[0];\n" +
            " let _ = x - 2.0 * b[1];\n" +
            " let _ = 2.0 * x + c[0];\n" +
            " let _ = x * 2.0 - c[1];\n" +
            "}\n"
        )
        self.assertEqual(mul_add_lib.float_mul_add_findings(self.rs_file)[1], [])


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
            + "*** Update File: crate/src/old.rs\n"
            + "*** Move to: crate/src/moved.rs\n@@\n"
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

    def test_two_updated_files_make_one_block_and_two_finding_records(self) -> None:
        self.write_source("a * 0.5 + c")
        other = self.package / "src/other.rs"
        _ = other.write_text("fn other(x: f32) { let _ = x * 2.0 + 1.0; }\n")
        result = self.patch(
            "*** Update File: crate/src/lib.rs\n@@\n"
            + "*** Update File: crate/src/other.rs\n@@\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 1)
        decision = cast(dict[str, object], json.loads(result.stdout))
        self.assertEqual(decision["systemMessage"],
                         "mul_add: 2 float multiply-add expression(s) in "
                         + "crate/src/lib.rs, crate/src/other.rs")
        reason = cast(str, decision["reason"])
        self.assertLess(reason.index("lib.rs"), reason.index("other.rs"))
        context = cast(dict[str, str], decision["hookSpecificOutput"])
        self.assertEqual(context["additionalContext"],
                         reason.removesuffix(" The edit was applied.").replace("; ", "\n"))
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
                           + "*** Update File: crate/src/other.rs\n@@\n*** End Patch\n"},
        })
        output = io.StringIO()
        main = cast(Callable[[], None], runpy.run_path(str(HOOK))["main"])
        first = mul_add_lib.float_mul_add_findings(self.rs_file)
        with (patch.dict(os.environ, self.environment),
              patch.object(sys, "stdin", io.StringIO(payload)), redirect_stdout(output),
              patch.object(mul_add_lib, "float_mul_add_findings",
                           side_effect=[first, RuntimeError("later file")])):
            main()
        message = cast(dict[str, object], json.loads(output.getvalue()))
        self.assertEqual(message, {"systemMessage": "mul_add hook error: RuntimeError: later file"})
        self.assertFalse((self.state / "blocks.jsonl").exists())


if __name__ == "__main__":
    _ = unittest.main()
