"""Check workspace TOML formatting and cached formatting failures."""

from __future__ import annotations

import unittest
from scripts.direct import verify_lint_scope_test_support as support


class VerifyLintScopeTests(support.VerifyLintScopeFixture):
    def test_taplo_uses_workspace_config_when_lint_starts_in_a_member_directory(self) -> None:
        result = self.invoke_verify("lint", "sample", cwd=self.root / "src")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_workspace_taplo_check()

    def test_taplo_failure_fails_lint_and_names_the_unformatted_file(self) -> None:
        result = self.invoke_verify("lint", "sample", cwd=self.root / "src", taplo_status=7)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_workspace_taplo_check()
        self.assertIn("Cargo.toml is not formatted", result.stdout + result.stderr)

    def test_unformatted_toml_remains_a_cached_tree_failure(self) -> None:
        cache = self.enable_cache()
        result = self.invoke_verify("lint", "sample", cwd=self.root, taplo_status=1)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Cargo.toml is not formatted", result.stdout + result.stderr)
        self.assert_workspace_taplo_check()
        self.assertEqual(len(list(cache.glob("*.fail"))), 1)

        result = self.invoke_verify("lint", "sample", cwd=self.root)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FAIL (recorded)", result.stdout)
        self.assertEqual(self.taplo_calls(), [])


if __name__ == "__main__":
    _ = unittest.main()
