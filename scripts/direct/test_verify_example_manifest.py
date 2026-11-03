"""Exercise real Bash example gating and pass recording with private Cargo tools."""

from __future__ import annotations

import unittest

from scripts.direct.verify_examples_test_support import SeededExampleWorkspaceFixture


class ExampleManifestTests(SeededExampleWorkspaceFixture):
    def test_true_example_with_test_module_is_refused(self) -> None:
        manifest = self.package(manifest_extra='\n[[example]]\nname = "demo"\ntest = true\n')
        example = self.example(source="#[cfg(test)]\nmod tests {}\nfn main() {}\n")

        result = self.verify("test", "sample", "--features", "default")

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(
            result.stderr,
            self.refusal("demo", example, 1, "#[cfg(test)]")
            + "\n\n" + self.enabled_example_refusal("demo", manifest) + "\n",
        )
        self.assertEqual(self.calls_made(), [])
        self.assertEqual(self.metadata_count(), 1)

    def test_test_enabled_example_without_marker_is_refused_for_package_tests(self) -> None:
        manifest = self.package(manifest_extra='\n[[example]]\nname = "demo"\ntest = true\n')
        _ = self.example(source="fn main() {}\n")

        result = self.verify("test", "sample")

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(result.stderr, self.enabled_example_refusal("demo", manifest) + "\n")
        self.assertEqual(self.calls_made(), [])
        self.assertEqual(self.metadata_count(), 1)

    def test_refusals_follow_metadata_package_then_target_order(self) -> None:
        _ = (self.root / "Cargo.toml").write_text(
            '[workspace]\nmembers = ["alpha", "beta"]\nresolver = "2"\n'
        )
        alpha_manifest = self.package(
            "alpha", root=self.root / "alpha",
            manifest_extra=(
                '\n[[example]]\nname = "first"\ntest = true\n'
                + '\n[[example]]\nname = "second"\ntest = true\n'
            ),
        )
        beta_manifest = self.package(
            "beta", root=self.root / "beta",
            manifest_extra='\n[[example]]\nname = "third"\ntest = true\n',
        )
        first = self.example("first", root=self.root / "alpha", source="#[test]\nfn check() {}\nfn main() {}\n")
        _ = self.example("second", root=self.root / "alpha", source="fn main() {}\n")
        _ = self.example("third", root=self.root / "beta", source="fn main() {}\n")

        result = self.verify("final")

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "\n\n".join([
            self.refusal("first", first, 1, "#[test]", package="alpha"),
            self.enabled_example_refusal("first", alpha_manifest),
            self.enabled_example_refusal("second", alpha_manifest),
            self.enabled_example_refusal("third", beta_manifest),
        ]) + "\n")
        self.assertEqual(self.calls_made(), [])
        self.assertEqual(self.metadata_count(), 1)

    def test_two_offenders_have_separate_blocks(self) -> None:
        _ = self.package()
        first = self.example("first", source="#[cfg(test)]\nmod tests {}\nfn main() {}\n")
        second = self.example("second", source="fn main() {}\n#[test]\nfn check() {}\n")

        result = self.verify("test", "sample")

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertCountEqual(
            result.stderr.rstrip("\n").split("\n\n"),
            [
                self.refusal("first", first, 1, "#[cfg(test)]"),
                self.refusal("second", second, 2, "#[test]"),
            ],
        )
        self.assertEqual(self.calls_made(), [])
        self.assertEqual(self.metadata_count(), 1)

    def test_final_checks_every_workspace_member_before_workspace_gate(self) -> None:
        _ = (self.root / "Cargo.toml").write_text('[workspace]\nmembers = ["alpha", "beta"]\nresolver = "2"\n')
        alpha_root = self.root / "alpha"
        beta_root = self.root / "beta"
        _ = self.package("alpha", root=alpha_root)
        _ = self.package("beta", root=beta_root)
        alpha = self.example("first", root=alpha_root, source="#[cfg(test)]\nmod tests {}\nfn main() {}\n")
        beta = self.example("second", root=beta_root, source="fn main() {}\n#[test]\nfn check() {}\n")

        result = self.verify("final")

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertCountEqual(
            result.stderr.rstrip("\n").split("\n\n"),
            [
                self.refusal("first", alpha, 1, "#[cfg(test)]", package="alpha"),
                self.refusal("second", beta, 2, "#[test]", package="beta"),
            ],
        )
        self.assertEqual(self.calls_made(), [])
        self.assertEqual(self.metadata_count(), 1)


if __name__ == "__main__":
    _ = unittest.main()
