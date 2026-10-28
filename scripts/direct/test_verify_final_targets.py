#!/usr/bin/env python3
"""Keep examples checked, but exclude their link targets from the final test run."""

from __future__ import annotations

import unittest

from scripts.direct import test_verify_untested_examples as example_workspace


class FinalTargetTests(example_workspace.ExampleWorkspaceFixture):
    def workspace_with_example(self, source: str = "fn main() {}\n") -> None:
        _ = (self.root / "Cargo.toml").write_text(
            '[workspace]\nmembers = ["sample"]\nresolver = "2"\n'
        )
        _ = self.package(root=self.root / "sample")
        _ = self.example(root=self.root / "sample", source=source)
        self.environment["NEXTEST_TEST_THREADS"] = "2"

    def test_final_checks_examples_and_tests_only_libraries_binaries_and_tests(self) -> None:
        self.workspace_with_example()

        result = self.verify("final")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls_made(), [
            "check --workspace --all-targets",
            "nextest --version",
            "nextest run --workspace --tests",
        ])

    def test_final_accepts_a_workspace_with_only_a_binary_target(self) -> None:
        self.workspace_with_example()
        source = self.root / "sample" / "src"
        (source / "lib.rs").unlink()
        _ = (source / "main.rs").write_text("fn main() {}\n")

        result = self.verify("final")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls_made(), [
            "check --workspace --all-targets",
            "nextest --version",
            "nextest run --workspace --tests",
        ])

    def test_final_refuses_test_enabled_example_without_marker_before_any_build(self) -> None:
        self.workspace_with_example()
        manifest = self.root / "sample" / "Cargo.toml"
        with manifest.open("a") as stream:
            _ = stream.write('\n[[example]]\nname = "demo"\ntest = true\n')

        result = self.verify("final")

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(result.stderr, self.enabled_example_refusal("demo", manifest) + "\n")
        self.assertEqual(self.calls_made(), [])
        self.assertEqual(self.metadata_count(), 1)

    def test_final_accepts_test_disabled_example_without_marker(self) -> None:
        self.workspace_with_example()
        with (self.root / "sample" / "Cargo.toml").open("a") as stream:
            _ = stream.write('\n[[example]]\nname = "demo"\ntest = false\n')

        result = self.verify("final")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls_made()[-1], "nextest run --workspace --tests")

    def test_failed_workspace_check_stops_before_nextest(self) -> None:
        self.workspace_with_example()
        self.environment["FAKE_CHECK_EXIT"] = "42"

        result = self.verify("final")

        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls_made(), ["check --workspace --all-targets"])

    def test_example_test_is_refused_before_any_build(self) -> None:
        self.workspace_with_example("fn main() {}\n#[test]\nfn hidden_test() {}\n")

        result = self.verify("final")

        self.assert_refused(
            result, "demo", self.root / "sample" / "examples" / "demo.rs", 2, "#[test]"
        )


if __name__ == "__main__":
    _ = unittest.main()
