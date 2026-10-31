"""Real verify.sh token build behavior."""
from __future__ import annotations

import unittest
from scripts.direct.verify_token_wait_test_support import VerifyTokenWaitFixture


class VerifyTokenBuildTests(VerifyTokenWaitFixture):
    def test_build_makes_the_packages_binaries_under_the_token_and_prints_their_paths(self) -> None:
        # A smoke or live-shot helper ran raw `cargo build` before it launched
        # the app, and the user approved it by hand (2026-10-23).
        environment = self.build_stub([{"name": "sample", "kind": ["lib"]},
                                       {"name": "sample-app", "kind": ["bin"]},
                                       {"name": "demo", "kind": ["example"]}])
        result = self.build(environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"binary: {self.root / 'target' / 'debug' / 'sample-app'}\n", result.stdout)
        self.assertNotIn("demo", result.stdout)
        self.assertEqual((self.root / "cargo-calls").read_text(), "build --workspace --bin sample-app\n")
        self.assertIn("claim: token cargo acquired", (self.root / "board" / "board.log").read_text())
        self.assertFalse((self.root / "board" / "locks" / "cargo.d").exists())

    def test_build_of_an_example_turns_on_its_required_features(self) -> None:
        environment = self.build_stub([{"name": "sample-app", "kind": ["bin"]},
                                       {"name": "demo", "kind": ["example"], "required-features": ["extra"]}])
        result = self.build(environment, "--example", "demo", "--release", "--features", "fast")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.root / "cargo-calls").read_text(),
                         "build --workspace --example demo --release --features sample/fast,sample/extra\n")
        self.assertIn(f"binary: {self.root / 'target' / 'release' / 'examples' / 'demo'}\n", result.stdout)

    def test_build_refuses_a_package_with_no_binary_and_a_target_it_lacks(self) -> None:
        environment = self.build_stub([{"name": "sample", "kind": ["lib"]}])
        result = self.build(environment)
        self.assertEqual(result.returncode, 2)
        self.assertIn("package sample has no binary; name an example with --example <name>", result.stderr)
        result = self.build(environment, "--bin", "other")
        self.assertEqual(result.returncode, 2)
        self.assertIn("package sample has no bin other", result.stderr)
        result = self.build(environment, "--bin", "a", "--example", "b")
        self.assertEqual(result.returncode, 2)
        self.assertFalse((self.root / "cargo-calls").exists())



if __name__ == "__main__":
    _ = unittest.main()
