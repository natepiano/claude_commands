"""Check repo dev-feature arguments through real verify.sh routes."""

from __future__ import annotations

import unittest

from scripts.direct.verify_dev_features_test_support import (
    DEV_ARGUMENTS, DevFeatureFixture, add_dev_features,
)


class VerifyDevFeatureExecutionTests(DevFeatureFixture):
    def test_test_and_filtered_test_keep_dev_features_on_the_nextest_invocation(self) -> None:
        add_dev_features(self.root)
        for extra, expression in (((), "package(sample)"),
                                  (("--filter", "changed_test"), "package(sample) & test(changed_test)")):
            with self.subTest(arguments=extra):
                self.clear_calls()
                result = self.verify("test", "sample", *extra)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.calls_made(), [
                    "nextest --version",
                    f"nextest run --no-fail-fast --workspace --lib -E {expression} --features {DEV_ARGUMENTS}",
                ])

    def test_final_uses_the_same_dev_features_for_check_and_nextest(self) -> None:
        add_dev_features(self.root)
        result = self.verify("final")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls_made(), [
            f"check --workspace --all-targets --features {DEV_ARGUMENTS}",
            "nextest --version",
            f"nextest run --workspace --tests --features {DEV_ARGUMENTS}",
        ])


if __name__ == "__main__":
    _ = unittest.main()
