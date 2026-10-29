"""Final gate commits checks."""
from __future__ import annotations

from pathlib import Path
import unittest

from scripts.direct.verify_final_pass_test_support import FinalPassFixture


class FinalPassTests(FinalPassFixture):
    def test_committed_tree_change_during_final_writes_no_record(self) -> None:
        self.environment["EDIT_DURING_TEST"] = "1"
        cargo = Path(self.environment["HOME"]) / "stubs/cargo"
        text = cargo.read_text().replace(
            'printf "// changed during the test run\\n" >> "$PACKAGE_ROOT/${EDIT_DURING_TEST_PATH:-src/lib.rs}"',
            'printf "// changed during the test run\\n" >> "$PACKAGE_ROOT/${EDIT_DURING_TEST_PATH:-src/lib.rs}"\n'
            + '    git add src/lib.rs\n    git commit --quiet -m changed',
        )
        _ = cargo.write_text(text)
        before = self.git("rev-parse", "HEAD^{tree}")
        result = self.verify("final")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotEqual(self.git("rev-parse", "HEAD^{tree}"), before)
        self.assertEqual(self.pass_files(), [])
        self.assertIn("the tree changed while final ran", result.stdout)


if __name__ == "__main__":
    _ = unittest.main()
