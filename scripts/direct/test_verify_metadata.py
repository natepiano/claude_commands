"""Compare the metadata CLI with outputs captured from verify.sh at phase start."""

from __future__ import annotations

import json
from pathlib import Path
import shlex
import subprocess
import sys
from typing import TypedDict, cast
import unittest


DIRECT = Path(__file__).resolve().parent
FIXTURES = DIRECT / "fixtures/verify_metadata"
REPOSITORY = DIRECT.parents[1]


class GoldenMetadataCase(TypedDict):
    fixture: str
    constant: str
    arguments: list[str]
    stdout: str
    stderr: str
    exit_code: int


class GoldenMetadataBaseline(TypedDict):
    baseline_commit: str
    baseline_source: str
    cases: list[GoldenMetadataCase]


def golden_cases() -> list[GoldenMetadataCase]:
    baseline = cast(GoldenMetadataBaseline, json.loads((FIXTURES / "golden.json").read_text()))
    return baseline["cases"]


class VerifyMetadataTests(unittest.TestCase):
    def run_helper(self, fixture: str, requests: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(DIRECT / "verify_metadata.py"), *requests],
            input=(FIXTURES / fixture).read_text(),
            cwd=REPOSITORY,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )

    def values(self, output: str) -> dict[str, str]:
        assignments = shlex.split(output)
        result: dict[str, str] = {}
        for assignment in assignments:
            self.assertIn("=", assignment, output)
            name, value = assignment.split("=", 1)
            self.assertRegex(name, r"^[A-Z_]+$")
            self.assertNotIn(name, result, output)
            result[name] = value
        return result

    def request(self, case: GoldenMetadataCase) -> str:
        # Public names confirmed by impl's phase-1 board status, entry 8.
        names = {
            "MEMBER_PY": "member",
            "EXAMPLE_FEATURES_PY": "example-features",
            "TEST_TARGETS_PY": "test-targets",
            "BUILD_TARGETS_PY": "build-targets",
            "UNTESTED_EXAMPLES_PY": "untested-examples",
            "WORKSPACE_ROOT": "workspace-root",
        }
        return ":".join([names[case["constant"]], *case["arguments"]])

    def test_each_request_preserves_the_inline_helpers_outputs_and_refusals(self) -> None:
        for case in golden_cases():
            request = self.request(case)
            with self.subTest(fixture=case["fixture"], request=request):
                result = self.run_helper(case["fixture"], [request])
                self.assertEqual(result.returncode, case["exit_code"], result.stdout + result.stderr)
                self.assertEqual(result.stderr, case["stderr"])
                if case["exit_code"]:
                    self.assertEqual(result.stdout, case["stdout"])
                else:
                    name = request.split(":", 1)[0].upper().replace("-", "_")
                    self.assertEqual(self.values(result.stdout), {name: case["stdout"].rstrip("\n")})

    def test_one_process_answers_multiple_requests_from_one_metadata_document(self) -> None:
        for fixture in ("workspace.json", "no_tests.json", "gated_examples.json"):
            selected: dict[str, GoldenMetadataCase] = {}
            for case in golden_cases():
                if case["fixture"] == fixture and case["exit_code"] == 0:
                    name = self.request(case).split(":", 1)[0]
                    if name not in selected:
                        selected[name] = case
            with self.subTest(fixture=fixture):
                result = self.run_helper(fixture, [self.request(case) for case in selected.values()])
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(result.stderr, "")
                self.assertEqual(self.values(result.stdout), {
                    name.upper().replace("-", "_"): case["stdout"].rstrip("\n")
                    for name, case in selected.items()
                })

    def test_target_directory_preserves_the_build_helpers_output_with_shell_metacharacters(self) -> None:
        baseline = next(case for case in golden_cases()
                        if case["constant"] == "BUILD_TARGETS_PY" and case["exit_code"] == 0)
        target_line = next(line for line in baseline["stdout"].splitlines() if line.startswith("target "))
        result = self.run_helper(baseline["fixture"], ["target-directory"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.values(result.stdout), {"TARGET_DIRECTORY": target_line.removeprefix("target ")})


if __name__ == "__main__":
    _ = unittest.main()
