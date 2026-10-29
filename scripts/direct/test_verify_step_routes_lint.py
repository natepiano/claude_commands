"""Real verify step routing: lint."""
from __future__ import annotations

from pathlib import Path
import unittest
from typing import cast

from scripts.direct.verify_step_routes_test_support import VerifyStepRoutesFixture


class VerifyStepRoutesTests(VerifyStepRoutesFixture):
    def test_lint_formats_locally_and_judges_on_mac_without_fix_passes(self) -> None:
        result = self.run_verify("lint", "sample")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = self.mac_rows()
        verbs: list[str] = []
        for row in rows:
            words = cast(list[str], row["args"])
            verbs.append(words[words.index("--verb") + 1])
            self.assertNotIn("--fix", words)
        self.assertEqual(verbs, ["mend-check", "clippy"])
        local = [event["args"] for event in self.fixture.cargo_events() if event["args"][:1] != ["metadata"]]
        self.assertTrue(any("fmt" in words for words in local), local)
        self.assertFalse(any("mend" in words or "clippy" in words for words in local), local)
        self.assert_removed()

    def test_new_hold_during_formatting_keeps_noncompiling_work_protected(self) -> None:
        (Path(self.fixture.environment["BUILD_HOLD_DIR"]) / "other").unlink()
        cargo = self.fixture.bin_directory / "cargo"
        source = cargo.read_text().replace('arguments = sys.argv[1:]', '''arguments = sys.argv[1:]
if "fmt" in arguments:
    record = json.loads((Path(os.environ["VERIFY_STEPS_DIR"]) / (os.environ["BUILDLOG_CALL_ID"] + ".json")).read_text())
    (Path(os.environ["PACKAGE_ROOT"]) / "fmt-state").write_text(record["state"])
    (Path(os.environ["BUILD_HOLD_DIR"]) / "other").write_text("new hold")
''')
        _ = cargo.write_text(source)
        self.fixture.write_executable(self.fixture.bin_directory / "taplo", '''
import json
import os
from pathlib import Path
record = json.loads((Path(os.environ["VERIFY_STEPS_DIR"]) / (os.environ["BUILDLOG_CALL_ID"] + ".json")).read_text())
(Path(os.environ["PACKAGE_ROOT"]) / "taplo-state").write_text(record["state"])
''')
        result = self.run_verify("lint", "sample")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.fixture.root / "fmt-state").read_text(), "local_noncompiling")
        self.assertEqual((self.fixture.root / "taplo-state").read_text(), "local_noncompiling")
        self.assertTrue(self.mac_rows())
        self.assert_removed()

if __name__ == "__main__":
    _ = unittest.main()
