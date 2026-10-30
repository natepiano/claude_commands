"""Exercise real Bash example gating and pass recording with private Cargo tools."""

from __future__ import annotations

import unittest

from scripts.direct.verify_examples_test_support import SeededExampleWorkspaceFixture


class ExampleEditTests(SeededExampleWorkspaceFixture):
    def test_a_write_to_the_session_folder_in_the_worktree_keeps_the_pass(self) -> None:
        _ = self.package()
        _ = (self.root / "session" / "board.log").write_text("")
        self.environment.update({
            "TREE_DIRTY": "1", "TREE_DIRTY_PATHS": "src/lib.rs session/board.log",
            "EDIT_DURING_TEST": "1", "EDIT_DURING_TEST_PATH": "session/board.log",
        })
        result = self.verify("test", "sample")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("the tree changed while test ran", result.stdout)
        self.assertEqual(len(list((self.root / "session" / "verify_cache").glob("*.pass"))), 1)

    def test_edit_during_the_test_run_records_no_pass(self) -> None:
        _ = self.package()
        self.environment.update({"TREE_DIRTY": "1", "EDIT_DURING_TEST": "1"})
        result = self.verify("test", "sample")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("the tree changed while test ran, so this result was not recorded", result.stdout)
        self.assertEqual(list((self.root / "session" / "verify_cache").glob("*.pass")), [])

        self.environment["EDIT_DURING_TEST"] = ""
        result = self.verify("test", "sample")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("PASS (recorded)", result.stdout)
        self.assertEqual(len(list((self.root / "session" / "verify_cache").glob("*.pass"))), 1)


if __name__ == "__main__":
    _ = unittest.main()
