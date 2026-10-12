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
            samples: list[float] = []
            for _ in range(5):
                started = time.perf_counter()
                _, findings = mul_add_lib.float_mul_add_findings(self.rs_file)
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
        self.assertIn("for a * 0.5 + c", cast(str, self.decision(payload=payload)["reason"]))

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
                         "crate/src/lib.rs:3: write a.mul_add(2.0, c) for a * 2.0 + c "
                         + "(clippy::suboptimal_flops) The edit was applied.")
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
                         "crate/src/lib.rs:3: write a.mul_add(2.0, c) for a * 2.0 + c "
                         + "(clippy::suboptimal_flops) The edit was applied.")
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

    def test_float_mul_add_findings_keeps_any_written_line_of_an_expression(self) -> None:
        _ = self.rs_file.write_text(
            "fn sample(a: f32, c: f32) {\n"
            + "    let _ = a\n        * 0.5\n        + c;\n}\n"
        )
        for written_line in (2, 3, 4):
            with self.subTest(written_line=written_line):
                scope, findings = mul_add_lib.float_mul_add_findings(
                    self.rs_file, frozenset({written_line})
                )
                self.assertTrue(scope.enabled)
                self.assertEqual([finding.line for finding in findings], [2])
        for written in (frozenset[int](), frozenset({1, 5})):
            with self.subTest(written=written):
                _, findings = mul_add_lib.float_mul_add_findings(self.rs_file, written)
                self.assertEqual(findings, [])

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
                         "crate/src/lib.rs:3: write a.mul_add(2.0, c) for a * 2.0 + c "
                         + "(clippy::suboptimal_flops) The edit was applied.")
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

    def test_two_updated_files_make_one_block_and_two_finding_records(self) -> None:
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
                           + "".join("+" + line + "\n" for line in self.rs_file.read_text().removesuffix("\n").split("\n"))
                           + "*** Update File: crate/src/other.rs\n@@\n"
                           + "".join("+" + line + "\n" for line in other.read_text().removesuffix("\n").split("\n"))
                           + "*** End Patch\n"},
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
