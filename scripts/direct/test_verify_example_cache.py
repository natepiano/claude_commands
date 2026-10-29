"""Exercise real Bash example gating and pass recording with private Cargo tools."""

from __future__ import annotations

import unittest

from scripts.direct.verify_examples_test_support import SeededExampleWorkspaceFixture


class ExampleCacheTests(SeededExampleWorkspaceFixture):
    def test_recorded_pass_does_not_hide_new_test(self) -> None:
        _ = self.package()
        example = self.example(source="fn main() {}\n")
        first = self.verify("test", "sample")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(len(list((self.root / "session" / "verify_cache").glob("*.pass"))), 1)
        first_calls = self.calls_made()
        _ = example.write_text("#[cfg(test)]\nmod tests {}\nfn main() {}\n")

        result = self.verify("test", "sample")

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(result.stderr, self.refusal("demo", example, 1, "#[cfg(test)]") + "\n")
        self.assertNotIn("PASS (recorded)", result.stdout + result.stderr)
        self.assertEqual(self.calls_made(), first_calls)
        self.assertEqual(self.metadata_count(), 2)


if __name__ == "__main__":
    _ = unittest.main()
