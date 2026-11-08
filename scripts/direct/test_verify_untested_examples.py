"""Exercise real Bash example gating and pass recording with private Cargo tools."""

from __future__ import annotations

import unittest
from typing import override
from scripts.build_queue.admission_test_support import caller_admission

# Existing suites import these fixture names from this module.
from scripts.direct.verify_examples_test_support import (
    ExampleWorkspaceFixture as ExampleWorkspaceFixture,
    FIXED_RUSTC as FIXED_RUSTC, VERIFY as VERIFY,
    rust_tool_path as rust_tool_path, workspace_metadata as workspace_metadata,
    SeededExampleWorkspaceFixture,
)


class UntestedExampleTests(SeededExampleWorkspaceFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = caller_admission(self, self.root, self.environment)

    def test_clean_package_passes_gate(self) -> None:
        _ = self.package()
        _ = self.example(source="fn main() {}\n")

        result = self.verify("test", "sample", "--features", "default")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(any(call.startswith("nextest run ") for call in self.calls_made()))
        self.assertEqual(self.metadata_count(), 1)


if __name__ == "__main__":
    _ = unittest.main()
