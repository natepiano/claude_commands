"""Check UI review shots, prompts, defects and failure records."""

from __future__ import annotations

import unittest
from scripts.direct import review_launcher_test_support as support
from pathlib import Path
from scripts.direct.review_launcher_test_support import TEST_HANG_GUARD_SECS as TEST_HANG_GUARD_SECS
from typing import cast
import json
import subprocess


class ReviewLauncherPassTests(support.ReviewLauncherFixture):
    def test_ui_pool_reviewed_turn_records_ux_kind_without_pass_calls_at_either_start_site(self) -> None:
        session = self.start_implementation("ui-no-pass")
        self.finish_implementation(session)
        _ = (session / "ux_shots_1.txt").write_text("shot.png\n", encoding="utf-8")
        environment = self.pool_fixture(reviewer_type="ui")
        self.trace_pass_recorder(environment)
        ready = session / "ready"
        ready.touch()
        for early in ("", str(ready)):
            with self.subTest(early=early):
                result = subprocess.run(self.pool_command(session, reviewer_type="ui", lens="ux", early=early),
                                        env=environment, capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.call_records("pass-calls.jsonl"), [])
                self.assertEqual((session / "review_status_ux").read_text().strip(), "reviewed")
                self.assertTrue(all(record["kind"] == "ux" and record["status"] == "reviewed"
                                    for record in self.turn_records(session)))
                self.assertTrue(self.turn_records(session))

    def test_ui_pool_failed_turn_records_error_without_pass_calls(self) -> None:
        session = self.start_implementation("ui-error-no-pass")
        self.finish_implementation(session)
        _ = (session / "ux_shots_1.txt").write_text("shot.png\n", encoding="utf-8")
        environment = self.pool_fixture(reviewer_type="ui")
        environment["STUB_TURN_FAILURE"] = "1"
        self.trace_pass_recorder(environment)
        result = subprocess.run(self.pool_command(session, reviewer_type="ui", lens="ux"),
                                env=environment, capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.call_records("pass-calls.jsonl"), [])
        self.assertEqual((session / "review_status_ux").read_text().strip(), "error")
        record, = self.turn_records(session)
        self.assertEqual((record["kind"], record["status"]), ("ux", "error"))

    def test_ui_pool_refuses_non_ux_lens_before_allocation_or_turn(self) -> None:
        session = self.start_implementation("ui-wrong-lens")
        self.finish_implementation(session)
        environment = self.pool_fixture(reviewer_type="ui")
        for lens in ("", "contract", "adversary", "craft"):
            with self.subTest(lens=lens):
                result = subprocess.run(self.pool_command(session, reviewer_type="ui", lens=lens),
                                        env=environment, capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stderr, "ERROR: --pool ui requires lens ux.\n")
        self.assertFalse((self.root / "pool-calls.jsonl").exists())
        self.assertFalse((self.root / "mesh-calls.jsonl").exists())

    def test_ui_pool_allocates_without_a_reset_and_sends_each_shot_on_the_requested_check(self) -> None:
        session = self.start_implementation("ui-shots")
        self.finish_implementation(session)
        shots = [str(self.root / "shot one.png"), str(self.root / "café.png")]
        _ = (session / "ux_shots_1.txt").write_text("unused.png\n", encoding="utf-8")
        _ = (session / "ux_shots_2.txt").write_text(shots[0] + "\r\n\n" + shots[1], encoding="utf-8")
        environment = self.pool_fixture(reviewer_type="ui")
        result = subprocess.run(self.pool_command(session, reviewer_type="ui", lens="ux", pass_index=2),
                                env=environment, capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        allocation = self.call_records("pool-calls.jsonl")[0]
        self.assertFalse(allocation["fresh"])
        self.assertEqual(allocation["type"], "ui")
        self.assertEqual(allocation["lens"], "ux")
        self.assertEqual(allocation["pass"], "2")
        calls = [cast("list[str]", json.loads(line)) for line in
                 (self.root / "mesh-calls.jsonl").read_text().splitlines()]
        follow, = [call for call in calls if call[0] == "follow"]
        self.assertEqual([follow[index + 1] for index, value in enumerate(follow) if value == "--image"], shots)
        self.assertEqual((session / "review_slot_ux").read_text().strip(), "pool_ui1")
        record, = self.turn_records(session)
        self.assertEqual((record["reviewer"], record["kind"], record["pass"]), ("ui1", "ux", 2))

    def test_ui_pool_failed_allocation_records_error_without_a_turn(self) -> None:
        session = self.start_implementation("ui-allocation-error")
        self.finish_implementation(session)
        _ = (session / "ux_shots_1.txt").write_text("shot.png\n", encoding="utf-8")
        environment = self.pool_fixture(reviewer_type="ui")
        environment["STUB_POOL_FAILURE"] = "1"
        result = subprocess.run(self.pool_command(session, reviewer_type="ui", lens="ux"),
                                env=environment, capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual([call["verb"] for call in self.call_records("pool-calls.jsonl")], ["allocate"])
        self.assertFalse(self.call_records("pool-calls.jsonl")[0]["fresh"])
        self.assertFalse((self.root / "mesh-calls.jsonl").exists())
        self.assertEqual((session / "review_status_ux").read_text().strip(), "error")
        self.assertEqual(self.turn_records(session)[0]["status"], "error")

    def test_ui_pool_turn_sends_the_prompt_as_written(self) -> None:
        session = self.start_implementation("ui-prompt")
        self.finish_implementation(session)
        _ = (session / "ux_shots_1.txt").write_text("shot.png\n", encoding="utf-8")
        environment = self.pool_fixture(reviewer_type="ui")
        environment["STUB_LEASE_ORDINAL"] = "2"
        command = self.pool_command(session, reviewer_type="ui", lens="ux")
        original = Path(command[8]).read_text()
        result = subprocess.run(command, env=environment, capture_output=True,
                                text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((Path(environment["STUB_POOL_MESH_DIR"]) / "follow_message.txt").read_text(), original)
        self.assertEqual([call["verb"] for call in self.call_records("pool-calls.jsonl")],
                         ["allocate", "release"])
        self.assertEqual(list(session.glob("review_message_*")), [])

    def test_ui_pool_missing_shots_file_records_error_without_allocation(self) -> None:
        session = self.start_implementation("ui-missing-shots")
        self.finish_implementation(session)
        result = subprocess.run(self.pool_command(session, reviewer_type="ui", lens="ux"),
                                env=self.pool_fixture(reviewer_type="ui"), capture_output=True,
                                text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("UI shots file is unreadable", result.stderr)
        self.assertFalse((self.root / "pool-calls.jsonl").exists())
        self.assertFalse((self.root / "mesh-calls.jsonl").exists())
        self.assertEqual(self.turn_records(session)[0]["status"], "error")

    def test_ui_pool_empty_shots_file_records_error_without_allocation(self) -> None:
        session = self.start_implementation("ui-empty-shots")
        self.finish_implementation(session)
        _ = (session / "ux_shots_1.txt").write_text("\n", encoding="utf-8")
        result = subprocess.run(self.pool_command(session, reviewer_type="ui", lens="ux"),
                                env=self.pool_fixture(reviewer_type="ui"), capture_output=True,
                                text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("UI shots file has no paths", result.stderr)
        self.assertFalse((self.root / "pool-calls.jsonl").exists())
        self.assertFalse((self.root / "mesh-calls.jsonl").exists())
        self.assertEqual(self.turn_records(session)[0]["status"], "error")

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
        result = subprocess.run(self.pool_command(session, reviewer_type="ui", lens="ux"), env=environment,
                                capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.turn_records(session)[0]["raised"], 3)

    def test_ux_pass_with_only_table_headers_records_zero_raised(self) -> None:
        session = self.start_implementation("ux-pass-count")
        self.finish_implementation(session)
        _ = (session / "ux_shots_1.txt").write_text("shot.png\n", encoding="utf-8")
        environment = self.pool_fixture(reviewer_type="ui")
        environment["STUB_FINDINGS"] = "Verdict: pass\n\n| Shot | Rule | Defect | Fix |\n| --- | --- | --- | --- |\n"
        result = subprocess.run(self.pool_command(session, reviewer_type="ui", lens="ux"), env=environment,
                                capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.turn_records(session)[0]["raised"], 0)


if __name__ == "__main__":
    _ = unittest.main()
