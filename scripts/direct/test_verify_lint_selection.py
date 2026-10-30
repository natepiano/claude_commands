"""Check judgment-only lint, feature refusals and example target selection."""

from __future__ import annotations

import unittest

from scripts.direct import verify_lint_scope_test_support as support


class VerifyLintScopeTests(support.VerifyLintScopeFixture):
    def test_check_lint_only_judges_without_fixes(self) -> None:
        _ = self.verify("lint", "--workspace", "--check")
        self.assertEqual(self.calls("clippy"), [[
            "--workspace", "--all-targets", "--all-features", "--", "-D", "warnings",
        ]])
        self.assertEqual(self.calls("mend"), [["--all-targets", "--workspace", "--fail-on-warn"]])
        self.assertEqual(self.calls("+nightly"), [["fmt", "--version"], ["fmt", "--all", "--check"]])
        self.assertEqual(self.calls("doc"), [])
        self.assert_workspace_taplo_check()

    def test_lint_rejects_features_now_owned_by_doc(self) -> None:
        result = self.invoke_verify("lint", "sample", "--features", "a", cwd=self.root)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("--features", result.stdout + result.stderr)
        self.assertEqual(self.calls("mend"), [])
        self.assertEqual(self.calls("clippy"), [])
        self.assertEqual(self.calls("doc"), [])
        self.assertEqual(self.calls("+nightly"), [])

    def test_example_keeps_its_single_target_and_required_features(self) -> None:
        for example, features in (("plain", []), ("featured", ["--features", "sample/a"])):
            with self.subTest(example=example):
                _ = self.verify("example", "sample", example)
                self.assertEqual(self.calls("clippy"), [[
                    "--workspace", "--example", example, *features, "--", "-D", "warnings"
                ]])
                self.assertEqual(self.calls("mend"), [])
                self.assertEqual(self.calls("doc"), [])
                self.assertEqual(self.calls("+nightly"), [])
                self.assertEqual(self.taplo_calls(), [])



if __name__ == "__main__":
    _ = unittest.main()
