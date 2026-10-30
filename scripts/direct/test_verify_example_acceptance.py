"""Exercise real Bash example gating and pass recording with private Cargo tools."""

from __future__ import annotations

import unittest

from scripts.direct.verify_examples_test_support import SeededExampleWorkspaceFixture


class ExampleAcceptanceTests(SeededExampleWorkspaceFixture):
    def test_commented_attribute_is_not_an_offender(self) -> None:
        _ = self.package()
        _ = self.example(source="// #[cfg(test)]\nfn main() {}\n")

        result = self.verify("test", "sample")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(any(call.startswith("nextest run ") for call in self.calls_made()))
        self.assertEqual(self.metadata_count(), 1)

    def test_test_disabled_example_without_marker_is_accepted_for_package_tests(self) -> None:
        _ = self.package(manifest_extra='\n[[example]]\nname = "demo"\ntest = false\n')
        _ = self.example(source="fn main() {}\n")

        result = self.verify("test", "sample")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(any(call.startswith("nextest run ") for call in self.calls_made()))


if __name__ == "__main__":
    _ = unittest.main()
