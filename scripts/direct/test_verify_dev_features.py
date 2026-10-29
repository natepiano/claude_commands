"""Check repo dev features through verify.sh with private Cargo command stubs."""

from __future__ import annotations

from pathlib import Path
from typing import override
import unittest

from scripts.direct import test_verify_doc as doc_fixture
from scripts.direct import test_verify_lint_scope as lint_fixture
from scripts.direct import test_verify_token_wait as build_fixture
from scripts.direct import test_verify_untested_examples as workspace_fixture


DEV_LIST = '["bevy/dynamic_linking", "engine/dev", "bevy/dynamic_linking"]'
DEV_ARGUMENTS = "bevy/dynamic_linking,engine/dev"


def add_dev_features(root: Path, value: str = DEV_LIST) -> None:
    with (root / "Cargo.toml").open("a") as manifest:
        _ = manifest.write(f"\n[workspace.metadata.dev]\nfeatures = {value}\n")


class VerifyDevFeatureTests(workspace_fixture.ExampleWorkspaceFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.package()
        self.environment["NEXTEST_TEST_THREADS"] = "2"

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
                result = self.verify("check", "sample")
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(result.stderr, "verify.sh: workspace.metadata.dev.features must be a list of strings\n")
                self.assertEqual(self.calls_made(), [])
                self.assertFalse(self.metadata_calls.exists())


class VerifyBuildDevFeatureTests(unittest.TestCase):
    def test_debug_build_merges_work_order_example_and_repo_features_but_release_does_not(self) -> None:
        fixture = build_fixture.VerifyTokenWaitTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        environment = fixture.build_stub([
            {"name": "sample-app", "kind": ["bin"]},
            {"name": "demo", "kind": ["example"], "required-features": ["extra"]},
        ])
        add_dev_features(fixture.root, '["bevy/dynamic_linking", "sample/extra", "sample/extra"]')
        calls = fixture.root / "cargo-calls"
        for arguments, expected in (
            ((), "build --workspace --bin sample-app --features bevy/dynamic_linking,sample/extra\n"),
            (("--example", "demo", "--features", "fast,extra,bevy/dynamic_linking"),
             "build --workspace --example demo --features sample/fast,sample/extra,bevy/dynamic_linking\n"),
            (("--release",), "build --workspace --bin sample-app --release\n"),
            (("--example", "demo", "--release", "--features", "fast"),
             "build --workspace --example demo --release --features sample/fast,sample/extra\n"),
        ):
            with self.subTest(arguments=arguments):
                calls.unlink(missing_ok=True)
                result = fixture.build(environment, *arguments)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(calls.read_text(), expected)


class VerifyDocDevFeatureTests(unittest.TestCase):
    def test_doc_merges_work_order_features_with_repo_features_without_package_rewriting(self) -> None:
        fixture = doc_fixture.VerifyDocTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        add_dev_features(fixture.repo)
        _ = fixture.git("add", "Cargo.toml")
        result = fixture.verify("--features", "alpha/extra,bevy/dynamic_linking")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = fixture.cargo_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["args"], [
            "doc", "--no-deps", "--jobs", "8", "--workspace", "--features",
            "alpha/extra,bevy/dynamic_linking,engine/dev",
        ])


class VerifyLintDevFeatureTests(unittest.TestCase):
    def test_lint_keeps_its_existing_commands_when_repo_dev_features_are_set(self) -> None:
        fixture = lint_fixture.VerifyLintScopeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        baseline = fixture.invoke_verify("lint", "sample", cwd=fixture.root)
        self.assertEqual(baseline.returncode, 0, baseline.stdout + baseline.stderr)
        original = fixture.events.read_bytes()
        fixture.events.unlink()
        add_dev_features(fixture.root)
        result = fixture.invoke_verify("lint", "sample", cwd=fixture.root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(fixture.events.read_bytes(), original)
        self.assertTrue(any(arguments[0] == "clippy" and "--all-features" in arguments
                            for arguments in fixture.cargo_calls()))


if __name__ == "__main__":
    _ = unittest.main()
