"""Check repo dev-feature arguments through real verify.sh routes."""

from __future__ import annotations

import unittest

from scripts.direct.verify_dev_features_test_support import (
    DEV_ARGUMENTS, DevFeatureFixture, add_dev_features, parse_dev_features,
)


class VerifyDevFeatureTests(DevFeatureFixture):
    def test_check_adds_repo_features_and_keeps_dependency_feature_names(self) -> None:
        add_dev_features(self.root)
        result = self.verify("check", "sample")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls_made(), [f"check --workspace --lib --bins --features {DEV_ARGUMENTS}"])

    def test_work_order_features_join_repo_features_once_after_qualification(self) -> None:
        add_dev_features(self.root, '["bevy/dynamic_linking", "sample/extra", "sample/extra"]')
        result = self.verify("check", "sample", "--features", "extra,bevy/dynamic_linking")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls_made(), [
            "check --workspace --lib --bins --features sample/extra,bevy/dynamic_linking",
        ])

    def test_malformed_repo_features_refuse_before_any_cargo_call(self) -> None:
        manifest = self.root / "Cargo.toml"
        original = manifest.read_text()
        for value in ('"fast"', '["fast", 2]', '{fast = true}', "false"):
            with self.subTest(value=value):
                _ = manifest.write_text(original)
                add_dev_features(self.root, value)
                # One complete refusal pins parsing before Cargo/metadata. The
                # remaining type variants exercise the same real parser directly.
                result = (self.verify("check", "sample") if value == '"fast"'
                          else parse_dev_features(manifest, self.environment))
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(result.stderr, "verify.sh: workspace.metadata.dev.features must be a list of strings\n")
                self.assertEqual(self.calls_made(), [])
                self.assertFalse(self.metadata_calls.exists())


if __name__ == "__main__":
    _ = unittest.main()
