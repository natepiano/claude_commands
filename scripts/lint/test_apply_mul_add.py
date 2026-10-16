#!/usr/bin/env python3
"""Apply only the selected Clippy byte spans in a temporary workspace."""

from __future__ import annotations

from collections.abc import Sequence
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import TypedDict, final, override
import unittest


APPLIER = Path(__file__).with_name("apply_mul_add.py")


class ClippySuggestionSpan(TypedDict):
    file_name: str
    byte_start: int
    byte_end: int
    suggested_replacement: str | None
    suggestion_applicability: str


def suggestion(
    file_name: str, start: int, end: int, replacement: str | None,
    *, applicability: str = "MachineApplicable",
) -> ClippySuggestionSpan:
    return {
        "file_name": file_name, "byte_start": start, "byte_end": end,
        "suggested_replacement": replacement, "suggestion_applicability": applicability,
    }


def diagnostic(
    spans: list[ClippySuggestionSpan], *, lint: str = "clippy::suboptimal_flops",
    children: Sequence[ClippySuggestionSpan] = (),
) -> dict[str, object]:
    return {"reason": "compiler-message", "message": {
        "code": {"code": lint}, "spans": spans, "children": [{"spans": list(children)}],
    }}


@final
class ApplyMulAddTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.workspace = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()

    def source(self, text: str, *, name: str = "src/lib.rs") -> Path:
        path = self.workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(text, encoding="utf-8")
        return path

    def apply(
        self, *records: dict[str, object], complete: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        stream = self.root / "diagnostics.jsonl"
        completion: tuple[dict[str, object], ...] = (
            ({"reason": "build-finished", "success": False},) if complete else ()
        )
        _ = stream.write_text(
            "\n".join(json.dumps(record) for record in
                      (*records, *completion)) + "\n",
            encoding="utf-8",
        )
        return subprocess.run(
            [sys.executable, "-B", str(APPLIER), "--workspace-root", str(self.workspace), str(stream)],
            cwd=self.root, capture_output=True, text=True, check=False, timeout=10,
        )

    def assert_count(self, result: subprocess.CompletedProcess[str], count: int, files: int) -> None:
        self.assertEqual(result.returncode, 0 if count else 3, result.stdout + result.stderr)
        self.assertEqual(result.stdout, f"verify.sh: rewrote {count} multiply-add(s) in {files} file(s)\n")

    def test_single_child_suggestion_rewrites_the_relative_workspace_file(self) -> None:
        path = self.source("a * b + c\n")
        result = self.apply(diagnostic([], children=[suggestion("src/lib.rs", 0, 9, "a.mul_add(b, c)")]))
        self.assert_count(result, 1, 1)
        self.assertEqual(path.read_bytes(), b"a.mul_add(b, c)\n")

    def test_two_spans_use_original_offsets_despite_replacement_length_changes(self) -> None:
        path = self.source("a * b + c; x + y * z;\n")
        result = self.apply(diagnostic([
            suggestion(str(path), 0, 9, "a.mul_add(b, c)"),
            suggestion(str(path), 11, 20, "y.mul_add(z, x)"),
        ]))
        self.assert_count(result, 2, 1)
        self.assertEqual(path.read_bytes(), b"a.mul_add(b, c); y.mul_add(z, x);\n")

    def test_duplicate_lib_and_test_diagnostics_count_once(self) -> None:
        path = self.source("a * b + c\n")
        span = suggestion("src/lib.rs", 0, 9, "a.mul_add(b, c)")
        result = self.apply(diagnostic([span]), diagnostic([], children=[span]))
        self.assert_count(result, 1, 1)
        self.assertEqual(path.read_bytes(), b"a.mul_add(b, c)\n")

    def test_overlapping_span_is_skipped_and_reported(self) -> None:
        path = self.source("a * b + c * d + e\n")
        result = self.apply(diagnostic([
            suggestion("src/lib.rs", 0, 9, "a.mul_add(b, c)"),
            suggestion("src/lib.rs", 8, 17, "c.mul_add(d, e)"),
        ]))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout,
                         "verify.sh: rewrote 1 multiply-add(s) in 1 file(s) (1 skipped: overlapping)\n")
        self.assertEqual(path.read_bytes(), b"a.mul_add(b, c) * d + e\n")

    def test_other_lints_non_machine_applicable_and_null_replacements_are_ignored(self) -> None:
        for lint, applicability, replacement in (
            ("clippy::useless_format", "MachineApplicable", "other_lint_rewrite"),
            ("unused_imports", "MachineApplicable", ""),
            ("clippy::suboptimal_flops", "MaybeIncorrect", "a.mul_add(b, c)"),
            ("clippy::suboptimal_flops", "HasPlaceholders", "a.mul_add(b, c)"),
            ("clippy::suboptimal_flops", "MachineApplicable", None),
        ):
            with self.subTest(lint=lint, applicability=applicability, replacement=replacement):
                path = self.source("a * b + c\n")
                result = self.apply(diagnostic([
                    suggestion("src/lib.rs", 0, 9, replacement, applicability=applicability),
                ], lint=lint))
                self.assert_count(result, 0, 0)
                self.assertEqual(path.read_bytes(), b"a * b + c\n")

    def test_external_files_are_ignored_including_a_symlink_outside_the_workspace(self) -> None:
        outside = self.root / "dependency.rs"
        _ = outside.write_bytes(b"a * b + c\n")
        (self.workspace / "dependency.rs").symlink_to(outside)
        for name in (str(outside), "../dependency.rs", "dependency.rs"):
            with self.subTest(file_name=name):
                result = self.apply(diagnostic([suggestion(name, 0, 9, "a.mul_add(b, c)")]))
                self.assert_count(result, 0, 0)
                self.assertEqual(outside.read_bytes(), b"a * b + c\n")

    def test_utf8_prefix_keeps_byte_offsets_and_surrounding_text(self) -> None:
        prefix = '// café ∑ 🦀\nlet label = "é"; '
        path = self.source(prefix + "a * b + c;\n")
        start = len(prefix.encode("utf-8"))
        result = self.apply(diagnostic([
            suggestion("src/lib.rs", start, start + 9, "a.mul_add(b, c)"),
        ]))
        self.assert_count(result, 1, 1)
        self.assertEqual(path.read_bytes(), (prefix + "a.mul_add(b, c);\n").encode("utf-8"))

    def test_unchanged_replacement_does_not_write_the_file(self) -> None:
        path = self.source("a * b + c\n")
        os.utime(path, ns=(1_000_000_000, 1_000_000_000))
        before = path.stat().st_mtime_ns
        result = self.apply(diagnostic([suggestion("src/lib.rs", 0, 9, "a * b + c")]))
        self.assert_count(result, 0, 0)
        self.assertEqual(path.stat().st_mtime_ns, before)
        self.assertEqual(path.read_bytes(), b"a * b + c\n")

    def test_incomplete_stream_fails_before_applying_any_suggestion(self) -> None:
        path = self.source("a * b + c\n")
        result = self.apply(diagnostic([
            suggestion("src/lib.rs", 0, 9, "a.mul_add(b, c)"),
        ]), complete=False)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertIn("no build-finished record", result.stderr)
        self.assertEqual(path.read_bytes(), b"a * b + c\n")

    def test_complete_stream_without_applicable_spans_returns_nothing_applied(self) -> None:
        self.assert_count(self.apply(), 0, 0)

    def test_unreadable_source_exits_one_without_creating_the_missing_file(self) -> None:
        result = self.apply(diagnostic([suggestion("src/missing.rs", 0, 9, "a.mul_add(b, c)")]))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertFalse((self.workspace / "src/missing.rs").exists())


if __name__ == "__main__":
    _ = unittest.main()
