"""Exercise real Bash example gating and pass recording with private Cargo tools."""

from __future__ import annotations

import unittest

from scripts.direct.verify_examples_test_support import SeededExampleWorkspaceFixture


class ExampleFinalTests(SeededExampleWorkspaceFixture):
    def test_final_runs_workspace_check_and_tests_without_format(self) -> None:
        _ = self.package()
        self.environment["NEXTEST_TEST_THREADS"] = "2"

        result = self.verify("final")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls_made(), [
            "check --workspace --all-targets",
            "nextest --version",
            "nextest run --workspace --tests",
        ])


if __name__ == "__main__":
    _ = unittest.main()
