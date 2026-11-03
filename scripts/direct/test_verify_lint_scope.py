"""Check workspace and package lint commands through the real shell pipeline."""

from __future__ import annotations

import unittest

from scripts.direct import verify_lint_scope_test_support as support
from scripts.direct.verify_lint_scope_test_support import CARGO_STUB as CARGO_STUB


class VerifyLintScopeTests(support.VerifyLintScopeFixture):
    def test_unchanged_tree_runs_one_fix_pass_before_judgment_without_rustdoc(self) -> None:
        result = self.verify("lint", "--workspace")
        self.assert_fix_and_judgment_calls()
        self.assertEqual(self.lint_stages(), [
            ["+nightly", "fmt", "--all"],
            ["clippy", *self.calls("clippy")[0]],
            ["+nightly", "fmt", "--all"],
            ["clippy", *self.calls("clippy")[1]],
        ])
        self.assertIn("style-lint: clippy start", result.stdout.splitlines())
        self.assertIn("style-lint: clippy passed", result.stdout.splitlines())

    def test_lint_matches_ci_and_preserves_other_stages(self) -> None:
        _ = self.verify("lint", "sample")
        self.assert_workspace_taplo_check()
        self.assert_fix_and_judgment_calls()
        self.assertEqual(self.calls("mend"), [["--all-targets", "--workspace", "--fix"]])
        self.assertEqual(self.calls("+nightly"), [
            ["fmt", "--version"], ["fmt", "-p", "sample"],
            ["fmt", "--version"], ["fmt", "-p", "sample"],
        ])
        self.assertEqual(self.calls("doc"), [])

    def test_clippy_off_names_the_full_scope_without_running_it(self) -> None:
        self.write_config(clippy="off")
        result = self.verify("lint", "sample")
        skip_lines = [line for line in result.stdout.splitlines()
                      if line.startswith("SKIPPED:") and "clippy" in line]
        self.assertEqual(skip_lines, [
            "SKIPPED: cargo clippy --workspace --all-targets --all-features"
            + f" — clippy=off in {self.config}"
        ])
        self.assertEqual(self.calls("clippy"), [])
        self.assertEqual(len(self.calls("mend")), 1)
        self.assertEqual(self.calls("doc"), [])
        self.assert_workspace_taplo_check()



if __name__ == "__main__":
    _ = unittest.main()
