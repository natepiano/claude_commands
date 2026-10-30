"""Check repo dev-feature arguments through real verify.sh routes."""

from __future__ import annotations

import unittest

from scripts.direct.verify_dev_features_test_support import (
    DevFeatureFixture, add_dev_features,
)


class VerifyDevFeatureDefaultsTests(DevFeatureFixture):
    def test_missing_and_empty_repo_features_keep_the_existing_exact_argv(self) -> None:
        for dev_table in (False, True):
            if dev_table:
                add_dev_features(self.root, "[]")
            for extra, suffix in (((), ""), (("--features", "fast,fast"), " --features sample/fast,sample/fast")):
                with self.subTest(dev_table=dev_table, arguments=extra):
                    self.clear_calls()
                    result = self.verify("check", "sample", *extra)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(self.calls.read_text(), f"check --workspace --lib --bins{suffix}\n")


if __name__ == "__main__":
    _ = unittest.main()
