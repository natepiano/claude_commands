"""Check workspace and package lint commands through the real shell pipeline."""

from __future__ import annotations

import unittest

from scripts.direct import verify_lint_scope_test_support as support
from scripts.direct.verify_lint_scope_test_support import CARGO_STUB as CARGO_STUB


class VerifyLintScopeTests(support.VerifyLintScopeFixture):
    def test_cache_reads_each_tool_version_once_per_run_and_rejects_a_changed_version(self) -> None:
        _ = self.enable_cache()
        events = self.root / "version-events"
        self.environment["TEST_VERSION_EVENTS"] = str(events)
        for version, recorded in (("before", False), ("before", True), ("after", False)):
            with self.subTest(version=version, recorded=recorded):
                events.unlink(missing_ok=True)
                self.environment["TEST_VERSION_MEND"] = version
                result = self.verify("lint", "sample")
                self.assertEqual("(recorded)" in result.stdout, recorded)
                self.assertEqual(events.read_text().splitlines(), ["TAPLO", "MEND", "RUSTFMT", "CLIPPY"])
                self.assertEqual(self.clippy_ran(), not recorded)

    def test_uncached_lint_reads_only_the_formatter_availability_version(self) -> None:
        events = self.root / "version-events"
        self.environment["TEST_VERSION_EVENTS"] = str(events)
        _ = self.verify("lint", "sample")
        self.assertEqual(events.read_text().splitlines(), ["RUSTFMT"])

    def test_clean_tree_runs_one_judgment_without_repairs_or_rustdoc(self) -> None:
        result = self.verify("lint", "--workspace")
        self.assertEqual(self.calls("clippy"), [[
            "--workspace", "--all-targets", "--all-features", "--keep-going", "--", "-D", "warnings",
        ]])
        self.assertEqual(self.lint_stages(), [
            ["+nightly", "fmt", "--all"],
            ["clippy", *self.calls("clippy")[0]],
        ])
        self.assertIn("style-lint: clippy start", result.stdout.splitlines())
        self.assertIn("style-lint: clippy passed", result.stdout.splitlines())

    def test_lint_matches_ci_and_preserves_other_stages(self) -> None:
        self.environment["TEST_CLIPPY_STATUSES"] = "[101, 0]"
        _ = self.verify("lint", "sample")
        self.assert_workspace_taplo_check()
        self.assert_fix_and_judgment_calls()
        self.assertEqual(self.calls("mend"), [["--all-targets", "--workspace", "--fix"]])
        self.assertEqual(self.calls("+nightly"), [
            ["fmt", "--version"], ["fmt", "-p", "sample"],
            ["fmt", "-p", "sample"],
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
