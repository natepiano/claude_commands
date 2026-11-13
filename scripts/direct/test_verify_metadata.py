"""Compare the metadata CLI with outputs captured from verify.sh at phase start."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
from typing import TypedDict, cast
import unittest

from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lib.shell_test_support import shell_function, shell_functions


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
            timeout=TEST_HANG_GUARD_SECS,
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

    def test_workspace_requests_select_members_and_the_example_owner(self) -> None:
        result = self.run_helper("workspace.json", ["test-targets::", "build-targets:::", "example-features::gated"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        values = self.values(result.stdout)
        self.assertEqual(values["TEST_TARGETS"].splitlines(), ["--lib", "--bins", "--tests"])
        self.assertEqual([line for line in values["BUILD_TARGETS"].splitlines() if line.startswith("binary ")],
                         ["binary alpha-cli", "binary beta-cli", "binary solo-cli"])
        self.assertEqual(values["EXAMPLE_FEATURES"], "alpha/graphics,dep/window")

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


class VerifyShellHelperTests(unittest.TestCase):
    def test_waiter_hash_preserves_scope_without_python(self) -> None:
        for tool in ("sha256sum", "shasum"):
            executable = shutil.which(tool)
            if executable is None:
                continue
            with self.subTest(tool=tool), tempfile.TemporaryDirectory() as directory:
                folder = Path(directory)
                (folder / tool).symlink_to(executable)
                script = "\n".join((
                    "set -euo pipefail", 'PATH="$1"', 'PY=false',
                    shell_functions(DIRECT / "verify.sh", "hash_stream", "token_waiters_key"),
                    "CMD=lint; ARGS=(alpha --check); token_waiters_key",
                    "CMD=lint; ARGS=(beta --check); token_waiters_key",
                    "CMD=test; ARGS=(alpha --filter 'case_one'); token_waiters_key",
                ))
                result = subprocess.run(["bash", "-c", script, "hash-test", str(folder)],
                                        cwd=folder, text=True, capture_output=True,
                                        timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(result.returncode, 0, result.stderr)
                scopes = (("lint", "--check"), ("lint", "--check"),
                          ("test", "alpha", "--filter", "case_one"))
                expected = [hashlib.sha256("\0".join((str(folder.resolve()), *words)).encode()).hexdigest()[:16]
                            for words in scopes]
                self.assertEqual(result.stdout.splitlines(), expected)

    def test_routing_wait_records_shell_elapsed_time_without_python(self) -> None:
        source = (DIRECT / "verify.sh").read_text()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "seconds"
            script = "\n".join((
                "set -euo pipefail", "phase_wait_finish() { :; }", "PY=false",
                'BUILDLOG_HOLD_WAIT_FILE="$1"', shell_function(source, "collect_routing_wait"),
                "unset EPOCHREALTIME; EPOCHREALTIME=103.500000; ROUTING_WAIT_STARTED=100.250000",
                "collect_routing_wait", 'test -z "$ROUTING_WAIT_STARTED"',
                "EPOCHREALTIME=99.000000; ROUTING_WAIT_STARTED=100.250000; collect_routing_wait",
            ))
            result = subprocess.run(["bash", "-c", script, "clock-test", str(output)],
                                    capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(output.read_text().splitlines(), ["3.250000", "0.000000"])


if __name__ == "__main__":
    _ = unittest.main()
