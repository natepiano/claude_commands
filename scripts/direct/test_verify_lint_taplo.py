"""Check workspace TOML formatting and cached formatting failures."""

from __future__ import annotations

import unittest
from typing import override
from scripts.build_queue.admission_test_support import caller_admission
from scripts.direct import verify_lint_scope_test_support as support


class VerifyLintScopeTests(support.VerifyLintScopeFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = caller_admission(self, self.root, self.environment)

    def test_check_rejects_malformed_tracked_toml_with_literal_brackets(self) -> None:
        tracked, untracked = self.toml_glob_neighbors(untracked_text="value = 2\n")
        before = untracked.read_bytes(), untracked.stat().st_mtime_ns
        result = self.invoke_workspace_taplo("--check")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(tracked.name, result.stdout + result.stderr)
        self.assertIn("not properly formatted", result.stdout + result.stderr)
        self.assertEqual((untracked.read_bytes(), untracked.stat().st_mtime_ns), before)

    def test_silent_taplo_check_failure_has_a_diagnostic(self) -> None:
        self.write_shell(self.root / "bin/taplo", "exit 1\n")
        result = self.invoke_workspace_taplo("--check")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("not properly formatted", result.stdout + result.stderr)

    def test_fix_formats_tracked_toml_with_literal_brackets_and_preserves_untracked_neighbor(self) -> None:
        tracked, untracked = self.toml_glob_neighbors(untracked_text="value=2\n")
        before = untracked.read_bytes(), untracked.stat().st_mtime_ns
        result = self.invoke_workspace_taplo()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(tracked.read_text(), "value = 1\n")
        self.assertEqual((untracked.read_bytes(), untracked.stat().st_mtime_ns), before)

    def test_badly_formatted_untracked_unit_config_leaves_lint_green(self) -> None:
        self.use_real_toml_tools()
        unit = self.root / ".unit.toml"
        _ = unit.write_text('plan="standby"\nresponsibilities=[]\n')
        before = unit.read_bytes(), unit.stat().st_mtime_ns
        result = self.invoke_taplo_at(self.root / "src", "--check")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((unit.read_bytes(), unit.stat().st_mtime_ns), before)

    def test_badly_formatted_tracked_toml_with_spaces_and_newlines_still_fails_lint(self) -> None:
        self.use_real_toml_tools()
        tracked = self.root / "tracked file\nwith newline.toml"
        _ = tracked.write_text('value=1\n')
        self.run_git("add", "--force", "--", tracked.name)
        for arguments in (("lint", "sample"), ("lint", "--check")):
            with self.subTest(arguments=arguments):
                result = self.invoke_verify(*arguments, cwd=self.root / "src")
                diagnostics = (self.root / support.TAPLO_RESULTS_FILE).read_text(encoding="utf-8")
                failure = result.stdout + result.stderr + "\nTaplo results:\n" + diagnostics
                self.assertNotEqual(result.returncode, 0, failure)
                self.assertIn("not properly formatted", result.stdout + result.stderr, failure)

    def test_fix_mode_formats_tracked_toml_and_preserves_untracked_unit_config(self) -> None:
        self.use_real_toml_tools()
        tracked, untracked = self.root / "tracked.toml", self.root / ".unit.toml"
        _ = tracked.write_text('value=1\n')
        _ = untracked.write_text('plan="standby"\nresponsibilities=[]\n')
        self.run_git("add", "--force", "--", tracked.name)
        before = untracked.read_bytes(), untracked.stat().st_mtime_ns
        result = self.invoke_workspace_taplo()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(tracked.read_text(), 'value = 1\n')
        self.assertEqual((untracked.read_bytes(), untracked.stat().st_mtime_ns), before)

    def test_tracked_files_outside_taplo_include_or_inside_exclude_stay_untouched(self) -> None:
        self.use_real_toml_tools()
        _ = (self.root / "taplo.toml").write_text(
            'include = ["included.toml", "excluded.toml"]\nexclude = ["excluded.toml"]\n')
        included = self.root / "included.toml"
        excluded = self.root / "excluded.toml"
        outside = self.root / "nested/outside.toml"
        outside.parent.mkdir()
        for path in (included, excluded, outside):
            _ = path.write_text('value=1\n')
        self.run_git("add", "--force", "--", included.name, excluded.name, str(outside.relative_to(self.root)))
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (excluded, outside)}
        result = self.invoke_workspace_taplo()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(included.read_text(), 'value = 1\n')
        self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns) for path in before}, before)

    def test_no_tracked_toml_skips_taplo_without_scanning_untracked_files(self) -> None:
        self.use_real_toml_tools()
        self.run_git("rm", "--cached", "--", "Cargo.toml", "taplo.toml")
        _ = (self.root / ".unit.toml").write_text('plan="standby"\nresponsibilities=[]\n')
        self.write_shell(self.root / "bin/taplo", 'echo "taplo must not run" >&2\nexit 97\n')
        result = self.invoke_taplo_at(self.root, "--check")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("taplo must not run", result.stdout + result.stderr)
        result = self.invoke_workspace_taplo()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

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
