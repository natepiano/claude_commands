"""A lost cargo token must not prevent the next lint run."""
from __future__ import annotations

import unittest

from scripts.direct.verify_lint_cache_test_support import VerifyLintCacheFixture


class VerifyLintCacheTokenLossTests(VerifyLintCacheFixture):
    def test_status_125_is_not_cached_and_the_next_lint_runs(self) -> None:
        cache = self.dirty_cache()
        result = self.invoke_verify("lint", "sample", cwd=self.root, taplo_status=125)
        self.assertEqual(result.returncode, 125, result.stdout + result.stderr)
        self.assertEqual(list(cache.glob("*.fail")), [])
        self.assertEqual(list(cache.glob("*.pass")), [])
        result = self.invoke_verify("lint", "sample", cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("FAIL (recorded)", result.stdout)
        self.assertTrue(self.clippy_ran())
        self.assertEqual(len(list(cache.glob("*.pass"))), 1)


if __name__ == "__main__":
    _ = unittest.main()
