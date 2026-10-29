"""Check dev-feature arguments on the real build, doc and lint commands."""

from __future__ import annotations

import unittest

from scripts.direct import test_verify_doc as doc_fixture
from scripts.direct import verify_lint_scope_test_support as lint_fixture
from scripts.direct.verify_token_wait_test_support import VerifyTokenWaitFixture
from scripts.direct.verify_dev_features_test_support import add_dev_features, isolate_verify_inputs


class VerifyBuildDevFeatureTests(unittest.TestCase):
    def test_debug_build_merges_work_order_example_and_repo_features_but_release_does_not(self) -> None:
        fixture = VerifyTokenWaitFixture()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        environment = fixture.build_stub([
            {"name": "sample-app", "kind": ["bin"]},
            {"name": "demo", "kind": ["example"], "required-features": ["extra"]},
        ])
        isolate_verify_inputs(environment, fixture.root)
        # Feature composition does not need a director's token/board lifecycle.
        for name in ("UNIT_SESSION_DIR", "UNIT_RUN_DIR", "UNIT_TEAM_ROLE",
                     "PLAN_DELEGATE_BOARD_DIR", "PLAN_DELEGATE_TEAM_ROLE"):
            environment[name] = ""
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
        isolate_verify_inputs(fixture.environment, fixture.root)
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
        fixture = lint_fixture.VerifyLintScopeFixture()
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
