"""Check repo dev features through verify.sh with private Cargo command stubs."""

from __future__ import annotations

from typing import override
import unittest

from scripts.direct import test_verify_untested_examples as workspace_fixture
from scripts.direct.verify_dev_features_test_support import (
    DEV_ARGUMENTS, add_dev_features, isolate_verify_inputs, parse_dev_features,
)


class VerifyDevFeatureTests(workspace_fixture.ExampleWorkspaceFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.package()
        self.environment["NEXTEST_TEST_THREADS"] = "2"
        isolate_verify_inputs(self.environment, self.root)

    def clear_calls(self) -> None:
        self.calls.unlink(missing_ok=True)

    def test_check_adds_repo_features_and_keeps_dependency_feature_names(self) -> None:
        add_dev_features(self.root)
        result = self.verify("check", "sample")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls_made(), [f"check --workspace --lib --bins --features {DEV_ARGUMENTS}"])

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

    def test_work_order_features_join_repo_features_once_after_qualification(self) -> None:
        add_dev_features(self.root, '["bevy/dynamic_linking", "sample/extra", "sample/extra"]')
        result = self.verify("check", "sample", "--features", "extra,bevy/dynamic_linking")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls_made(), [
            "check --workspace --lib --bins --features sample/extra,bevy/dynamic_linking",
        ])

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
