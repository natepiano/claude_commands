"""Exercise real Bash example gating and pass recording with private Cargo tools."""

from __future__ import annotations

import unittest

from scripts.direct.verify_examples_test_support import SeededExampleWorkspaceFixture


class ExampleDependencyTests(SeededExampleWorkspaceFixture):
    def test_an_edit_to_a_member_the_package_is_not_built_from_keeps_its_pass(self) -> None:
        result = self.side_by_side_phases("beta/src/lib.rs")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("the tree changed while test ran", result.stdout)
        self.assertEqual(len(list((self.root / "session" / "verify_cache").glob("*.pass"))), 1)

        result = self.verify("test", "alpha")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS (recorded)", result.stdout)
        self.assertEqual(sum(call.startswith("nextest run ") for call in self.calls_made()), 1)

    def test_an_edit_to_a_path_dependency_during_the_run_records_no_pass(self) -> None:
        result = self.side_by_side_phases("gamma/src/lib.rs")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("the tree changed while test ran, so this result was not recorded", result.stdout)
        self.assertEqual(list((self.root / "session" / "verify_cache").glob("*.pass")), [])


if __name__ == "__main__":
    _ = unittest.main()
