"""Check UI review shots, prompts, defects and failure records."""

from __future__ import annotations

from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

import unittest
from scripts.direct import review_launcher_test_support as support
import subprocess


class ReviewLauncherPassTests(support.ReviewLauncherFixture):

    def test_plain_ux_defect_report_does_not_write_a_turn_record(self) -> None:
        session = self.start_implementation("ux-defect-count")
        self.finish_implementation(session)
        _ = (session / "ux_shots_1.txt").write_text("shot.png\n", encoding="utf-8")
        environment = self.environment()
        environment["STUB_FINDINGS"] = (
            "Verdict: defects\n\n"
            "| Shot | Rule | Defect | Fix |\n| --- | --- | --- | --- |\n"
            "| a.png | cables-drape-smoothly | sag crosses the face | lift the cable |\n"
            "| b.png | no rule | label cut at the right edge | widen the field\n"
            "| c.png | widgets-follow-macos | reads `on \\| off` as one word | split the label |\n\n"
            "Canonical candidates\n\n"
            "| Shot | Rule | Slot | Crop (full-size px) | Why it is clear |\n"
            "| --- | --- | --- | --- | --- |\n"
            "| /tmp/a.png | cables-drape-smoothly | bad | 40x40+1+1 | one cable |\n\n"
            "1. Other text\n"
        )
        result = subprocess.run(self.review_command(session, "ux", ""), env=environment,
                                capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.turn_records(session), [])

    def test_plain_ux_pass_does_not_write_a_turn_record(self) -> None:
        session = self.start_implementation("ux-pass-count")
        self.finish_implementation(session)
        _ = (session / "ux_shots_1.txt").write_text("shot.png\n", encoding="utf-8")
        environment = self.environment()
        environment["STUB_FINDINGS"] = "Verdict: pass\n\n| Shot | Rule | Defect | Fix |\n| --- | --- | --- | --- |\n"
        result = subprocess.run(self.review_command(session, "ux", ""), env=environment,
                                capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.turn_records(session), [])


if __name__ == "__main__":
    _ = unittest.main()
