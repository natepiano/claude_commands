"""Final gate script checks."""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
from scripts.direct.test_verify_untested_examples import VERIFY
import unittest

from scripts.direct.verify_final_pass_test_support import FinalPassFixture
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class FinalPassTests(FinalPassFixture):
    def test_query_refuses_a_changed_verify_script(self) -> None:
        result = self.verify("final")
        self.assertEqual(result.returncode, 0, result.stderr)
        folder = Path(self.environment["HOME"]) / "changed/direct"
        folder.mkdir(parents=True)
        (folder.parent / "lib").symlink_to(VERIFY.parent.parent / "lib", target_is_directory=True)
        _ = shutil.copyfile(VERIFY.parent / "final_pass.py", folder / "final_pass.py")
        changed = folder / "verify.sh"
        _ = changed.write_text(VERIFY.read_text() + "\n# Test script change.\n")
        answer = subprocess.run(["bash", str(changed), "final-pass"], cwd=self.root,
                                env=self.environment, text=True, capture_output=True, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(answer.returncode, 1, answer.stdout + answer.stderr)
        self.assertIn("verify.sh differs", answer.stderr)


if __name__ == "__main__":
    _ = unittest.main()
