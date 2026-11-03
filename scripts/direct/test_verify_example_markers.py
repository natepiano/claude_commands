"""Exercise real Bash example gating and pass recording with private Cargo tools."""

from __future__ import annotations

import unittest

from scripts.direct.verify_examples_test_support import SeededExampleWorkspaceFixture


class ExampleMarkerTests(SeededExampleWorkspaceFixture):
    def test_false_example_with_test_module_is_refused(self) -> None:
        _ = self.package(manifest_extra='\n[[example]]\nname = "demo"\ntest = false\n')
        example = self.example(source="fn main() {}\n\n#[cfg(test)]\nmod tests {}\n")

        self.assert_refused(self.verify("test", "sample"), "demo", example, 3, "#[cfg(test)]")

    def test_plain_test_attribute_is_refused(self) -> None:
        _ = self.package()
        example = self.example(source="fn main() {}\n  #[test]  \nfn check() {}\n")

        self.assert_refused(self.verify("test", "sample"), "demo", example, 2, "#[test]")

    def test_namespaced_test_attribute_is_refused(self) -> None:
        _ = self.package()
        example = self.example(source="#[tokio::test]\nasync fn check() {}\nfn main() {}\n")

        self.assert_refused(self.verify("test", "sample"), "demo", example, 1, "#[tokio::test]")

    def test_directory_example_scans_module_files(self) -> None:
        _ = self.package()
        directory = self.root / "examples" / "demo"
        directory.mkdir(parents=True)
        _ = (directory / "main.rs").write_text("mod hidden;\nfn main() {}\n")
        module = directory / "hidden.rs"
        _ = module.write_text("// first line\n  #[cfg(test)]\nmod tests {}\n")

        self.assert_refused(self.verify("test", "sample"), "demo", module, 2, "#[cfg(test)]")

    def test_explicit_example_name_scans_its_directory(self) -> None:
        _ = self.package(
            manifest_extra='\n[[example]]\nname = "renamed"\npath = "examples/demo/main.rs"\n'
        )
        directory = self.root / "examples" / "demo"
        directory.mkdir(parents=True)
        _ = (directory / "main.rs").write_text("mod hidden;\nfn main() {}\n")
        module = directory / "hidden.rs"
        _ = module.write_text("// first line\n#[test]\nfn check() {}\n")

        self.assert_refused(self.verify("test", "sample"), "renamed", module, 2, "#[test]")


if __name__ == "__main__":
    _ = unittest.main()
