"""Invalidate lint verdicts when any lint tool version changes."""

from __future__ import annotations

import json
import subprocess
import unittest
from unittest import mock

from scripts.direct import tree_watch
from scripts.direct.tree_watch_test_support import TreeWatchFixture
from scripts.direct.verify_lint_cache_test_support import VerifyLintCacheFixture
from scripts.direct.verify_lint_scope_test_support import CARGO_STUB


class VerifyLintToolVersionTests(VerifyLintCacheFixture):
    def check_version_change(self, tool: str) -> None:
        _ = self.dirty_cache()
        for old_status, new_status in ((0, 43), (43, 0)):
            with self.subTest(cached_status=old_status):
                self.environment["TEST_VERSION_" + tool] = f"{tool} before-{old_status}"
                self.environment["TEST_CLIPPY_STATUS"] = str(old_status)
                first = self.invoke_verify("lint", "sample", cwd=self.root)
                self.assertEqual(first.returncode, old_status, first.stdout + first.stderr)
                self.assertTrue(self.clippy_ran())
                repeated = self.invoke_verify("lint", "sample", cwd=self.root)
                self.assertEqual(repeated.returncode, old_status, repeated.stdout + repeated.stderr)
                self.assertIn("(recorded)", repeated.stdout)
                self.assertFalse(self.clippy_ran())

                self.environment["TEST_VERSION_" + tool] = f"{tool} after-{old_status}"
                self.environment["TEST_CLIPPY_STATUS"] = str(new_status)
                changed = self.invoke_verify("lint", "sample", cwd=self.root)
                self.assertEqual(changed.returncode, new_status, changed.stdout + changed.stderr)
                self.assertNotIn("(recorded)", changed.stdout)
                self.assertTrue(self.clippy_ran())

    def test_mend_version_invalidates_passes_and_failures(self) -> None:
        self.check_version_change("MEND")

    def test_rustfmt_version_invalidates_passes_and_failures(self) -> None:
        self.check_version_change("RUSTFMT")

    def test_clippy_version_invalidates_passes_and_failures(self) -> None:
        self.check_version_change("CLIPPY")

    def test_taplo_version_invalidates_passes_and_failures(self) -> None:
        self.check_version_change("TAPLO")

    def test_unknown_version_prevents_cache_reuse_and_publication(self) -> None:
        cache = self.dirty_cache()
        _ = self.verify("lint", "sample")
        self.assertEqual(len(list(cache.glob("*.pass"))), 1)
        for status in (0, 1):
            with self.subTest(version_status=status):
                self.write_executable(self.root / "bin/cargo", (
                    "import sys\n"
                    "if sys.argv[1:] == ['mend', '--version']:\n"
                    f"    raise SystemExit({status})\n"
                ) + CARGO_STUB)
                for _ in range(2):
                    result = self.verify("lint", "sample")
                    self.assertNotIn("(recorded)", result.stdout)
                    self.assertTrue(self.clippy_ran())
                    self.assertEqual(len(list(cache.glob("*.pass"))), 1)


class LintVersionKeyTests(TreeWatchFixture):
    def test_unknown_versions_preserve_the_input_watch(self) -> None:
        metadata = json.dumps(self.metadata)
        _, watched = tree_watch.TreeInputs(["--", "lint"], metadata).keys(cache=False)
        for error in (OSError("tool missing"), ValueError("empty version"),
                      subprocess.CalledProcessError(1, "cargo"), subprocess.TimeoutExpired("cargo", 10)):
            with self.subTest(error=type(error).__name__), \
                 mock.patch.object(tree_watch, "lint_tool_versions", side_effect=error):
                inputs = tree_watch.TreeInputs(["--", "lint"], metadata)
                self.assertEqual(inputs.keys(), ("", watched))

    def test_test_keys_and_input_watches_skip_lint_tool_queries(self) -> None:
        metadata = json.dumps(self.metadata)
        with mock.patch.object(tree_watch, "lint_tool_versions", side_effect=AssertionError("unexpected version query")):
            inputs = tree_watch.TreeInputs(["--package", "sample", "--", "test", "sample"], metadata)
            key, watched = inputs.keys()
            self.assertRegex(key, r"^[0-9a-f]{64}$")
            self.assertRegex(watched, r"^[0-9a-f]{64}$")
            _, lint_watch = tree_watch.TreeInputs(["--", "lint"], metadata).keys(cache=False)
            self.assertRegex(lint_watch, r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    _ = unittest.main()
