#!/usr/bin/env python3
"""Integration tests for the durable plan-delegate progress history."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import cast, override


SCRIPT = Path(__file__).with_name("progress_history.py")


class ProgressHistoryTests(unittest.TestCase):
    temporary: tempfile.TemporaryDirectory[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    history_dir: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    working_dir: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.history_dir = self.root / "history"
        self.working_dir = self.root / "bevy_hana_rubric"
        self.working_dir.mkdir()
        _ = subprocess.run(
            ["git", "init", "-b", "feature/rubric"],
            cwd=self.working_dir,
            check=True,
            capture_output=True,
            text=True,
        )

    @override
    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_command(self, *arguments: str, at: int) -> str:
        environment = os.environ.copy()
        environment["PLAN_DELEGATE_HISTORY_DIR"] = str(self.history_dir)
        environment["PLAN_DELEGATE_NOW_EPOCH"] = str(at)
        result = subprocess.run(
            ["python3", str(SCRIPT), *arguments],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        return result.stdout.strip()

    def run_failing_command(self, *arguments: str, at: int) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["PLAN_DELEGATE_HISTORY_DIR"] = str(self.history_dir)
        environment["PLAN_DELEGATE_NOW_EPOCH"] = str(at)
        return subprocess.run(
            ["python3", str(SCRIPT), *arguments],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )

    def start_run(self, name: str, started_at: int) -> Path:
        session_dir = self.root / name
        session_dir.mkdir()
        _ = self.run_command(
            "start-run",
            "--session-dir",
            str(session_dir),
            "--working-dir",
            str(self.working_dir),
            "--plan-doc",
            "docs/rubric.md",
            "--project-started-at",
            str(started_at),
            "--main-family",
            "codex",
            "--main-model",
            "gpt-main",
            "--main-effort",
            "xhigh",
            "--main-session-id",
            f"main-{name}",
            at=started_at,
        )
        return session_dir

    def start_phase_and_pass(self, session_dir: Path, started_at: int) -> None:
        _ = self.run_command(
            "start-phase",
            "--session-dir",
            str(session_dir),
            "--phase-id",
            "3",
            "--phase-title",
            "Retry handling",
            at=started_at,
        )
        _ = self.run_command(
            "start-pass",
            "--session-dir",
            str(session_dir),
            "--pass-kind",
            "fix",
            "--fix-pass",
            "2",
            "--activity",
            "correcting retry recovery",
            "--called-task",
            "delegate.escalation",
            "--called-family",
            "codex",
            "--called-model",
            "gpt-called",
            "--called-effort",
            "high",
            at=started_at + 10,
        )

    def complete_historical_run(self, index: int) -> None:
        started_at = 10_000 + index * 1_000
        session_dir = self.start_run(f"historical-{index}", started_at)
        self.start_phase_and_pass(session_dir, started_at)
        _ = self.run_command(
            "calibrate",
            "--session-dir",
            str(session_dir),
            "--candidate-percent",
            "65",
            at=started_at + 100,
        )
        _ = self.run_command(
            "progress",
            "--session-dir",
            str(session_dir),
            "--raw-percent",
            "65",
            "--percent",
            "65",
            "--activity",
            "correcting retry recovery",
            at=started_at + 100,
        )
        _ = self.run_command(
            "calibrate",
            "--session-dir",
            str(session_dir),
            "--candidate-percent",
            "65",
            at=started_at + 160,
        )
        _ = self.run_command(
            "progress",
            "--session-dir",
            str(session_dir),
            "--raw-percent",
            "65",
            "--percent",
            "65",
            "--activity",
            "correcting retry recovery",
            at=started_at + 160,
        )
        _ = self.run_command(
            "finish-pass",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed",
            at=started_at + 200,
        )
        _ = self.run_command(
            "finish-phase",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed",
            at=started_at + 400,
        )
        _ = self.run_command(
            "finish-run",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed",
            at=started_at + 410,
        )

    def test_header_and_calibration_use_completed_history(self) -> None:
        for index in range(5):
            self.complete_historical_run(index)

        started_at = 20_000
        session_dir = self.start_run("current", started_at)
        self.start_phase_and_pass(session_dir, started_at)
        calibration_text = self.run_command(
            "calibrate",
            "--session-dir",
            str(session_dir),
            "--candidate-percent",
            "65",
            at=started_at + 100,
        )
        parsed: object = json.loads(calibration_text)  # pyright: ignore[reportAny]
        calibration = cast(dict[str, object], parsed)
        self.assertEqual(calibration["sample_count"], 5)
        self.assertEqual(calibration["suggested_percent"], 25)
        self.assertEqual(calibration["apply_suggestion"], True)

        header = self.run_command(
            "progress",
            "--session-dir",
            str(session_dir),
            "--project-raw-percent",
            "80",
            "--project-percent",
            "80",
            "--phase-raw-percent",
            "65",
            "--phase-percent",
            "25",
            "--activity",
            "correcting retry recovery",
            at=started_at + 100,
        )
        self.assertEqual(
            header.splitlines(),
            [
                "**bevy_hana_rubric - feature/rubric**",
                "**80% complete - elapsed 01:40**",
                "",
                "**Phase 3: Retry handling**",
                "**25% complete - elapsed 01:40**",
                "**Fix 2 - correcting retry recovery - elapsed 01:30**",
            ],
        )

        _ = self.run_command(
            "calibrate",
            "--session-dir",
            str(session_dir),
            "--candidate-percent",
            "65",
            at=started_at + 160,
        )
        unchanged_header = self.run_command(
            "progress",
            "--session-dir",
            str(session_dir),
            "--project-raw-percent",
            "80",
            "--project-percent",
            "80",
            "--phase-raw-percent",
            "65",
            "--phase-percent",
            "25",
            "--activity",
            "correcting retry recovery",
            at=started_at + 160,
        )
        self.assertIn(
            "**80% complete - elapsed 02:40 - unchanged 01:00**",
            unchanged_header,
        )
        self.assertIn(
            "**25% complete - elapsed 02:40 - unchanged 01:00**",
            unchanged_header,
        )

        _ = self.run_command(
            "calibrate",
            "--session-dir",
            str(session_dir),
            "--candidate-percent",
            "65",
            at=started_at + 180,
        )
        independently_changed = self.run_command(
            "progress",
            "--session-dir",
            str(session_dir),
            "--project-raw-percent",
            "85",
            "--project-percent",
            "85",
            "--phase-raw-percent",
            "65",
            "--phase-percent",
            "25",
            "--activity",
            "correcting retry recovery",
            at=started_at + 180,
        )
        self.assertIn("**85% complete - elapsed 03:00**", independently_changed)
        self.assertIn(
            "**25% complete - elapsed 03:00 - unchanged 01:20**",
            independently_changed,
        )

        history_file = self.history_dir / "runs" / "current.jsonl"
        history_text = history_file.read_text(encoding="utf-8")
        self.assertIn('"model":"gpt-main"', history_text)
        self.assertIn('"model":"gpt-called"', history_text)
        self.assertIn('"raw_percent":65', history_text)
        self.assertIn('"percent":25', history_text)
        self.assertIn('"project_percent":80', history_text)
        self.assertIn('"phase_percent":25', history_text)
        self.assertIn('"decision_source":"calibrated"', history_text)
        self.assertIn('"suggested_percent":25', history_text)
        self.assertIn('"historical_bias_percentage_points":40.0', history_text)

        _ = self.run_command(
            "finish-pass",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed",
            at=started_at + 200,
        )
        _ = self.run_command(
            "finish-phase",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed",
            at=started_at + 400,
        )
        _ = self.run_command(
            "finish-run",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed",
            at=started_at + 410,
        )
        aggregate_text = self.run_command("aggregate", "--percent", "65", at=30_000)
        aggregate_parsed: object = json.loads(aggregate_text)  # pyright: ignore[reportAny]
        aggregate = cast(dict[str, object], aggregate_parsed)
        groups = cast(list[object], aggregate["groups"])
        group = cast(dict[str, object], groups[0])
        self.assertEqual(group["sample_count"], 6)
        decision_counts = cast(dict[str, object], group["decision_source_counts"])
        self.assertEqual(decision_counts["raw"], 5)
        self.assertEqual(decision_counts["calibrated"], 1)

    def test_legacy_progress_call_keeps_existing_header(self) -> None:
        started_at = 20_000
        session_dir = self.start_run("legacy", started_at)
        self.start_phase_and_pass(session_dir, started_at)
        header = self.run_command(
            "progress",
            "--session-dir",
            str(session_dir),
            "--raw-percent",
            "20",
            "--percent",
            "20",
            "--activity",
            "correcting retry recovery",
            at=started_at + 100,
        )
        self.assertEqual(
            header.splitlines(),
            [
                "**bevy_hana_rubric - feature/rubric**",
                "**Phase 3: Retry handling - elapsed 01:40**",
                "**Fix 2 - correcting retry recovery - elapsed 01:30**",
                "**20% complete**",
                "**Total elapsed 01:40**",
            ],
        )

    def test_aggregate_reports_raw_and_calibrated_error_fields(self) -> None:
        self.complete_historical_run(0)
        output = self.run_command("aggregate", "--percent", "65", at=30_000)
        parsed: object = json.loads(output)  # pyright: ignore[reportAny]
        aggregate = cast(dict[str, object], parsed)
        groups = cast(list[object], aggregate["groups"])
        self.assertEqual(len(groups), 1)
        group = cast(dict[str, object], groups[0])
        self.assertEqual(group["sample_count"], 1)
        self.assertIn("median_raw_absolute_error_percentage_points", group)
        self.assertIn("median_suggested_absolute_error_percentage_points", group)
        self.assertIn("median_reported_absolute_error_percentage_points", group)

    def test_override_requires_and_records_reason(self) -> None:
        for index in range(5):
            self.complete_historical_run(index)

        started_at = 20_000
        session_dir = self.start_run("override", started_at)
        self.start_phase_and_pass(session_dir, started_at)
        _ = self.run_command(
            "calibrate",
            "--session-dir",
            str(session_dir),
            "--candidate-percent",
            "65",
            at=started_at + 100,
        )
        failure = self.run_failing_command(
            "progress",
            "--session-dir",
            str(session_dir),
            "--raw-percent",
            "65",
            "--percent",
            "35",
            "--activity",
            "correcting retry recovery",
            at=started_at + 100,
        )
        self.assertNotEqual(failure.returncode, 0)
        self.assertIn("--override-reason is required", failure.stderr)

        reason = "three of four recovery checks now pass"
        _ = self.run_command(
            "progress",
            "--session-dir",
            str(session_dir),
            "--raw-percent",
            "65",
            "--percent",
            "35",
            "--activity",
            "correcting retry recovery",
            "--override-reason",
            reason,
            at=started_at + 100,
        )
        history_file = self.history_dir / "runs" / "override.jsonl"
        parsed_events: list[dict[str, object]] = []
        for line in history_file.read_text(encoding="utf-8").splitlines():
            parsed: object = json.loads(line)  # pyright: ignore[reportAny]
            parsed_events.append(cast(dict[str, object], parsed))
        progress_events = [
            event for event in parsed_events if event.get("event_type") == "progress_reported"
        ]
        self.assertEqual(len(progress_events), 1)
        progress_event = progress_events[0]
        self.assertEqual(progress_event["decision_source"], "override")
        self.assertEqual(progress_event["override_reason"], reason)
        self.assertEqual(progress_event["suggested_percent"], 25)
        self.assertEqual(progress_event["suggested_adjustment_percentage_points"], -40)
        self.assertEqual(progress_event["reported_adjustment_percentage_points"], -30)


if __name__ == "__main__":
    _ = unittest.main()
