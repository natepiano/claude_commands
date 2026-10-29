"""Check missing or panicking formatters cannot cache a lint judgment."""

from __future__ import annotations

import unittest
from scripts.direct import verify_lint_scope_test_support as support
from scripts.direct.verify_lint_scope_test_support import TAPLO_STUB as TAPLO_STUB


class VerifyLintScopeTests(support.VerifyLintScopeFixture):
    def test_missing_taplo_fails_before_stages_and_is_not_cached(self) -> None:
        cache = self.enable_cache()
        self.isolate_path_without_taplo()
        result = self.invoke_verify("lint", "sample", cwd=self.root)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(result.stderr.splitlines(), ["taplo is required"])
        self.assertEqual(self.calls("mend"), [])
        self.assertEqual(self.calls("+nightly"), [])
        self.assertEqual(self.calls("clippy"), [])
        self.assertEqual(self.calls("doc"), [])
        self.assertEqual(self.taplo_calls(), [])
        self.assertEqual(list(cache.glob("*.fail")), [])

        self.write_executable(self.root / "bin" / "taplo", TAPLO_STUB)
        _ = self.verify("lint", "sample")
        self.assert_workspace_taplo_check()
        self.assertEqual(len(list(cache.glob("*.pass"))), 1)

    def test_taplo_panic_on_linux_is_a_tooling_failure_and_is_not_cached(self) -> None:
        cache = self.enable_cache()
        self.write_shell(self.root / "bin" / "uname", 'printf "Linux\\n"\n')
        result = self.invoke_verify("lint", "sample", cwd=self.root, taplo_status=101)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("taplo panicked", result.stderr)
        self.assertIn("tooling failure", result.stderr)
        self.assert_workspace_taplo_check()
        self.assertEqual(list(cache.glob("*.fail")), [])

        _ = self.verify("lint", "sample")
        self.assert_workspace_taplo_check()
        self.assertEqual(len(list(cache.glob("*.pass"))), 1)

    def test_taplo_panic_on_macos_requests_unsandboxed_retry_and_is_not_cached(self) -> None:
        cache = self.enable_cache()
        self.write_shell(self.root / "bin" / "uname", 'printf "Darwin\\n"\n')
        result = self.invoke_verify("lint", "sample", cwd=self.root, taplo_status=101)
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("taplo panicked", result.stderr)
        self.assertIn("macOS sandbox", result.stderr)
        self.assertIn("Re-run the same command with the sandbox disabled", result.stderr)
        self.assert_workspace_taplo_check()
        self.assertEqual(list(cache.glob("*.fail")), [])

        _ = self.verify("lint", "sample")
        self.assert_workspace_taplo_check()
        self.assertEqual(len(list(cache.glob("*.pass"))), 1)


if __name__ == "__main__":
    _ = unittest.main()
