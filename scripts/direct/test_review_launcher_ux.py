"""Check UI review shots, prompts, defects and failure records."""

from __future__ import annotations

from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

import unittest
from scripts.direct import review_launcher_test_support as support
import subprocess


class ReviewLauncherPassTests(support.ReviewLauncherFixture):
    def test_ui_pool_refuses_before_any_review_for_every_lens_and_shot_state(self) -> None:
        # The old UI arm must never silently become a code review, even when
        # screenshots and an otherwise valid early-review request are present.
        for lens, shots, early in (("ux", "shot.png\n", False), ("ux", "", False),
                                   ("ux", None, False), ("ux", "shot.png\n", True),
                                   ("contract", "shot.png\n", False), ("", None, False),
                                   ("adversary", None, False), ("craft", None, False)):
            with self.subTest(lens=lens, shots=shots, early=early):
                session = self.start_implementation(f"ui-refused-{lens or 'closure'}-{len(list(self.root.glob('ui-refused-*')))}")
                self.finish_implementation(session)
                if shots is not None:
                    _ = (session / "ux_shots_1.txt").write_text(shots, encoding="utf-8")
                ready = session / "ready"
                ready.touch()
                environment = self.pool_fixture(reviewer_type="ui")
                self.trace_pass_recorder(environment)
                result = subprocess.run(self.pool_command(session, reviewer_type="ui", lens=lens,
                                                          early=str(ready) if early else ""),
                                        env=environment, capture_output=True, text=True, check=False,
                                        timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(len(result.stderr.splitlines()), 1)
                self.assertIn("fresh Claude helper", result.stderr)
                self.assertIn("<UXReview/>", result.stderr)
                self.assertFalse((self.root / "pool-calls.jsonl").exists())
                self.assertFalse((self.root / "mesh-calls.jsonl").exists())
                self.assertEqual(self.call_records("pass-calls.jsonl"), [])
                self.assertEqual(self.turn_records(session), [])
                self.assertFalse((session / "review_status_ux").exists())

    def test_ux_raised_counts_defect_rows_without_headers_candidates_or_numbered_text(self) -> None:
        session = self.start_implementation("ux-defect-count")
        self.finish_implementation(session)
        _ = (session / "ux_shots_1.txt").write_text("shot.png\n", encoding="utf-8")
        environment = self.pool_fixture(reviewer_type="ui")
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
        result = subprocess.run(self.review_command(session, "ux", "", self.open_worker(session)), env=environment,
                                capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.turn_records(session)[0]["raised"], 3)

    def test_ux_pass_with_only_table_headers_records_zero_raised(self) -> None:
        session = self.start_implementation("ux-pass-count")
        self.finish_implementation(session)
        _ = (session / "ux_shots_1.txt").write_text("shot.png\n", encoding="utf-8")
        environment = self.pool_fixture(reviewer_type="ui")
        environment["STUB_FINDINGS"] = "Verdict: pass\n\n| Shot | Rule | Defect | Fix |\n| --- | --- | --- | --- |\n"
        result = subprocess.run(self.review_command(session, "ux", "", self.open_worker(session)), env=environment,
                                capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.turn_records(session)[0]["raised"], 0)


if __name__ == "__main__":
    _ = unittest.main()
