"""Exercise real Bash example gating and pass recording with private Cargo tools."""

from __future__ import annotations

import unittest

from scripts.direct.verify_examples_test_support import SeededExampleWorkspaceFixture


class ExampleCommandTests(SeededExampleWorkspaceFixture):
    def test_filter_and_named_target_skip_gate(self) -> None:
        _ = self.package()
        _ = self.example(source="#[cfg(test)]\nmod tests {}\nfn main() {}\n")
        integration = self.root / "tests" / "smoke.rs"
        integration.parent.mkdir()
        _ = integration.write_text("#[test]\nfn smoke() {}\n")

        for args in (("test", "sample", "--filter", "smoke"), ("test", "sample", "smoke")):
            with self.subTest(args=args):
                result = self.verify(*args)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("holds a test", result.stdout + result.stderr)
        self.assertEqual(sum(call.startswith("nextest run ") for call in self.calls_made()), 2)

    def test_removed_example_test_runs_no_cargo(self) -> None:
        _ = self.package()
        _ = self.example(source="fn main() {}\n")

        result = self.verify("example-test", "sample", "demo")

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(
            result.stderr,
            "verify.sh: example-test is removed: examples carry no tests (user, 2026-10-04).\n",
        )
        self.assertEqual(self.calls_made(), [])
        self.assertEqual(self.metadata_count(), 0)


if __name__ == "__main__":
    _ = unittest.main()
