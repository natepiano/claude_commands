"""Merge gate suites contracts."""

from __future__ import annotations

import unittest
import os
import subprocess
from pathlib import Path
from typing import final
from unittest import mock
from scripts.production import merge_gate
from scripts.production.merge_gate_test_support import TEST_HANG_GUARD_SECS, gate_suites, wait_for_file
from scripts.production.merge_gate_suites_test_support import GateSuitesFixture


@final
class MergeGateTypesTests(GateSuitesFixture):
    @gate_suites("hooks")
    def test_types_owns_a_shared_slot_until_it_finishes(self) -> None:
        self.supply_jobserver(b"T")
        start = merge_gate.start_gate_job
        types_pids: list[int] = []

        def observed(suite: str, path: Path, argv: list[str], repo: Path,
                     environment: dict[str, str], log: Path, *,
                kind: merge_gate.GateJobKind = merge_gate.GateJobKind.TEST_MODULE) -> merge_gate.RunningGateJob:
            if suite == "types":
                self.assertEqual(self.jobserver_tokens(), b"")
            job = start(suite, path, argv, repo, environment, log, kind=kind)
            if suite == "types":
                types_pids.append(job.process.pid)
                wait_for_file(self.state / "types-ran")
            return job

        with mock.patch.object(merge_gate, "start_gate_job", side_effect=observed):
            result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(types_pids), 1)
        self.assertEqual(self.jobserver_tokens(), b"T")

    def test_the_type_check_venv_is_built_once_with_the_pinned_stubs(self) -> None:
        with mock.patch.dict(os.environ, self.environment):
            first, second = merge_gate.typecheck_python(), merge_gate.typecheck_python()
        venv = self.state / "runner" / "typecheck-venv"
        self.assertEqual((first, second), (venv / "bin" / "python", venv / "bin" / "python"))
        self.assertEqual([call["args"] for call in self.calls() if call["tool"] == "uv"],
                         [["venv", "--clear", str(venv)],
                          ["pip", "install", "--python", str(venv / "bin" / "python"),
                           *merge_gate.TYPECHECK_REQUIREMENTS]])

    def test_tests_only_runs_types_for_each_requested_directory(self) -> None:
        result = self.run_gate("--tests-only", "--type-dirs", "scripts/production", "scripts/hooks")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        type_call, = [call for call in self.calls() if call["tool"] == "basedpyright"]
        python = self.state / "runner/typecheck-venv/bin/python"
        self.assertEqual(type_call["args"], ["--pythonpath", str(python), "scripts/production", "scripts/hooks"])
        self.assertEqual(type_call["cwd"], str(self.checkout))
        self.assertIn("basedpyright: OK", result.stdout)
        self.assert_all_modules_started()
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
        self.assert_not_pushed()

    def test_tests_only_runs_types_with_hooks_in_the_default_scope(self) -> None:
        result = subprocess.run(
            [*self.gate_command(), "--tests-only", "--repo", str(self.checkout)],
            cwd=self.root, env=self.environment, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        type_call, = [call for call in self.calls() if call["tool"] == "basedpyright"]
        python = self.state / "runner/typecheck-venv/bin/python"
        self.assertEqual(type_call["args"], ["--pythonpath", str(python), "scripts"])
        self.assertEqual(type_call["cwd"], str(self.checkout))

    @gate_suites("lib")
    def test_a_type_check_venv_that_cannot_be_built_is_red_and_names_the_command(self) -> None:
        self.write(self.state / "uv-fails", "fail")
        result = self.run_gate()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("basedpyright: RED\ntype-check venv missing: stderr: fixture: no network; build it with: ", result.stdout)
        self.assertIn(" pip install --python ", result.stdout)
        self.assertEqual([call for call in self.calls() if call["tool"] == "basedpyright"], [])
        self.assertRegex(result.stdout, r"(?m)^lib: OK \d+\.\d+s$")
        self.assert_not_pushed()

    def test_type_failure_resets_merge_and_does_not_push(self) -> None:
        self.write(self.state / "types-red", "red")
        result = self.run_gate()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("fixture type error", result.stdout + result.stderr)
        self.assert_not_pushed()
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
        self.assertEqual(self.git(self.checkout, "status", "--porcelain"), "")


if __name__ == "__main__":
    _ = unittest.main()
