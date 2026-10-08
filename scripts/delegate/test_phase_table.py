#!/usr/bin/env python3
"""Tests for the phase table built from delegate state and durable events."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import cast, override
from unittest.mock import patch
from zoneinfo import ZoneInfo

from scripts.delegate import phase_table


SCRIPT = Path(__file__).with_name("phase_table.py")


def plan_text(*lines: str) -> str:
    return "\n".join((*lines, ""))


class PhaseTableTests(unittest.TestCase):
    temporary: tempfile.TemporaryDirectory[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    history_dir: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    working_dir: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    session_dir: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    plan: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.history_dir = self.root / "history"
        self.working_dir = self.root / "worktree"
        self.session_dir = self.root / "session"
        self.plan = self.working_dir / "docs" / "plan.md"
        self.plan.parent.mkdir(parents=True)
        self.session_dir.mkdir()

    def write_plan(self, text: str) -> None:
        _ = self.plan.write_text(text, encoding="utf-8")

    def write_state(self, phase: dict[str, object] | None) -> None:
        state = {
            "working_dir": str(self.working_dir),
            "plan_doc": "docs/plan.md",
            "project_plan_doc": str(self.plan),
            "phase": phase,
            "status": "active",
        }
        _ = (self.session_dir / "progress_history_state.json").write_text(
            json.dumps(state),
            encoding="utf-8",
        )

    def write_run(
        self,
        name: str,
        started_at: int,
        *events: dict[str, object],
    ) -> Path:
        path = self.history_dir / "runs" / f"{name}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        started = {
            "event_type": "run_started",
            "working_dir": str(self.working_dir),
            "plan_doc": "docs/plan.md",
            "run_started_at": started_at,
            "timestamp_epoch": started_at,
        }
        lines = [json.dumps(started), *(json.dumps(event) for event in events)]
        _ = path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def phase_event(
        self,
        event_type: str,
        phase: str,
        instance: str,
        at: int,
        **fields: object,
    ) -> dict[str, object]:
        return {
            "event_type": event_type,
            "phase_id": phase,
            "phase_instance_id": instance,
            "timestamp_epoch": at,
            **fields,
        }

    def build(self, at: int) -> phase_table.PhaseRecord:
        with patch.dict(
            os.environ,
            {
                "PLAN_DELEGATE_HISTORY_DIR": str(self.history_dir),
                "PLAN_DELEGATE_NOW_EPOCH": str(at),
            },
        ):
            return phase_table.build(self.session_dir)

    def assert_epoch(self, value: object, expected: int) -> None:
        self.assertIsInstance(value, datetime)
        moment = cast(datetime, value)
        self.assertIsNotNone(moment.tzinfo)
        self.assertIsNotNone(moment.utcoffset())
        self.assertEqual(moment.timestamp(), expected)

    def test_build_combines_repeated_phase_runs_and_predicts_from_history(self) -> None:
        self.write_plan(
            plan_text(
                "### Phase 1 — Archived without recorder events (`abc1234`)",
                "",
                "### Phase 2 — Repeated delivery  · status: done",
                "",
                "### Phase 3 — Current delivery  · status: todo",
                "",
                "### Phase 3 Review",
                "",
                "### Phase 4 — Later delivery  · status: todo",
            )
        )
        older_run = self.write_run(
            "older",
            1_000,
            self.phase_event("phase_started", "2", "old-2", 1_100),
            self.phase_event(
                "phase_finished",
                "2",
                "old-2",
                1_220,
                status="completed",
                phase_elapsed_seconds=120,
            ),
        )
        current_run = self.write_run(
            "current",
            2_000,
            self.phase_event("phase_started", "2", "new-2", 2_010),
            self.phase_event(
                "phase_finished",
                "2",
                "new-2",
                2_190,
                status="completed",
                phase_elapsed_seconds=180,
            ),
            self.phase_event("phase_started", "3", "new-3", 2_250),
            self.phase_event(
                "progress_reported",
                "3",
                "new-3",
                2_310,
                phase_percent=50,
                phase_elapsed_seconds=60,
                phase_calibration=None,
            ),
        )
        self.write_state(
            {
                "id": "3",
                "title": "Current delivery",
                "instance_id": "new-3",
                "started_at": 2_250,
                "status": "active",
            }
        )
        state_path = self.session_dir / "progress_history_state.json"
        before_state = state_path.read_bytes()
        before_runs = (older_run.read_bytes(), current_run.read_bytes())

        record = self.build(2_400)
        rows = {row.phase: row for row in record.phases}
        current = record.current
        self.assertIsInstance(current, phase_table.RunningPhase)
        assert isinstance(current, phase_table.RunningPhase)

        self.assertEqual(current.phase, "3")
        self.assert_epoch(current.started, 2_250)
        progress = current.progress
        self.assertIsInstance(progress, phase_table.Reported)
        assert isinstance(progress, phase_table.Reported)
        self.assertEqual(progress.percent, 50)
        projection = progress.eta
        self.assertIsInstance(projection, phase_table.ProjectedEta)
        assert isinstance(projection, phase_table.ProjectedEta)
        self.assert_epoch(projection.time, 2_370)
        self.assert_epoch(projection.earliest, 2_350)
        self.assert_epoch(projection.latest, 2_400)

        archived = rows["1"]
        self.assertIsInstance(archived, phase_table.DonePhase)
        assert isinstance(archived, phase_table.DonePhase)
        self.assertIsInstance(archived.times, phase_table.NotRecorded)
        repeated = rows["2"]
        self.assertIsInstance(repeated, phase_table.DonePhase)
        assert isinstance(repeated, phase_table.DonePhase)
        self.assertIsInstance(repeated.times, phase_table.Completed)
        assert isinstance(repeated.times, phase_table.Completed)
        self.assert_epoch(repeated.times.start, 1_100)
        self.assert_epoch(repeated.times.finish, 2_190)
        self.assertEqual(repeated.times.seconds, 300)
        self.assertIsInstance(rows["3"], phase_table.RunningPhase)
        later = rows["4"]
        self.assertIsInstance(later, phase_table.TodoPhase)
        assert isinstance(later, phase_table.TodoPhase)
        self.assertIsInstance(later.times, phase_table.Predicted)
        assert isinstance(later.times, phase_table.Predicted)
        self.assert_epoch(later.times.start, 2_430)
        self.assert_epoch(later.times.finish, 2_730)
        self.assertIsInstance(record.plan_finish, phase_table.PredictedFinish)
        assert isinstance(record.plan_finish, phase_table.PredictedFinish)
        self.assert_epoch(record.plan_finish.at, 2_730)
        self.assertEqual(state_path.read_bytes(), before_state)
        self.assertEqual(
            (older_run.read_bytes(), current_run.read_bytes()),
            before_runs,
        )

    def test_prediction_uses_current_projected_total_without_completed_phases(
        self,
    ) -> None:
        self.write_plan(
            plan_text(
                "### Phase 1 — Current delivery  · status: todo",
                "",
                "### Phase 2 — Later delivery  · status: todo",
            )
        )
        _ = self.write_run(
            "current",
            900,
            self.phase_event("phase_started", "1", "one", 1_000),
            self.phase_event(
                "progress_reported",
                "1",
                "one",
                1_100,
                phase_percent=50,
                phase_elapsed_seconds=100,
                phase_calibration=None,
            ),
        )
        self.write_state(
            {
                "id": "1",
                "title": "Current delivery",
                "instance_id": "one",
                "started_at": 1_000,
                "status": "active",
            }
        )

        record = self.build(1_150)
        later = record.phases[1]
        self.assertIsInstance(later, phase_table.TodoPhase)
        assert isinstance(later, phase_table.TodoPhase)
        self.assertIsInstance(later.times, phase_table.Predicted)
        assert isinstance(later.times, phase_table.Predicted)

        self.assert_epoch(later.times.start, 1_200)
        self.assert_epoch(later.times.finish, 1_400)
        self.assertIsInstance(record.plan_finish, phase_table.PredictedFinish)
        assert isinstance(record.plan_finish, phase_table.PredictedFinish)
        self.assert_epoch(record.plan_finish.at, 1_400)

    def test_prediction_is_unknown_without_history_or_a_current_report(self) -> None:
        self.write_plan(
            plan_text(
                "### Phase 1 — Current delivery  · status: todo",
                "",
                "### Phase 2 — Later delivery  · status: todo",
            )
        )
        _ = self.write_run(
            "current",
            900,
            self.phase_event("phase_started", "1", "one", 1_000),
        )
        self.write_state(
            {
                "id": "1",
                "title": "Current delivery",
                "instance_id": "one",
                "started_at": 1_000,
                "status": "active",
            }
        )

        record = self.build(1_150)
        current = record.current
        self.assertIsInstance(current, phase_table.RunningPhase)
        assert isinstance(current, phase_table.RunningPhase)
        self.assertIsInstance(current.progress, phase_table.NotReported)
        later = record.phases[1]
        self.assertIsInstance(later, phase_table.TodoPhase)
        assert isinstance(later, phase_table.TodoPhase)
        self.assertIsInstance(later.times, phase_table.NotPredicted)
        self.assertIsInstance(record.plan_finish, phase_table.UnknownFinish)

    def test_zero_percent_report_has_no_projection(self) -> None:
        self.write_plan(
            plan_text(
                "### Phase 1 — Current delivery  · status: todo",
                "",
                "### Phase 2 — Later delivery  · status: todo",
            )
        )
        _ = self.write_run(
            "current",
            900,
            self.phase_event("phase_started", "1", "one", 1_000),
            self.phase_event(
                "progress_reported",
                "1",
                "one",
                1_100,
                phase_percent=0,
                phase_elapsed_seconds=100,
                phase_calibration=None,
            ),
        )
        self.write_state(
            {
                "id": "1",
                "title": "Current delivery",
                "instance_id": "one",
                "started_at": 1_000,
                "status": "active",
            }
        )

        record = self.build(1_150)
        current = record.current
        self.assertIsInstance(current, phase_table.RunningPhase)
        assert isinstance(current, phase_table.RunningPhase)
        self.assertIsInstance(current.progress, phase_table.Reported)
        assert isinstance(current.progress, phase_table.Reported)
        self.assertEqual(current.progress.percent, 0)
        self.assertIsInstance(current.progress.eta, phase_table.NoEta)
        later = record.phases[1]
        self.assertIsInstance(later, phase_table.TodoPhase)
        assert isinstance(later, phase_table.TodoPhase)
        self.assertIsInstance(later.times, phase_table.NotPredicted)

    def test_reused_phase_number_uses_last_completed_title_family(self) -> None:
        self.write_plan(plan_text("### Phase 9 — Replacement delivery  · status: done"))
        _ = self.write_run(
            "history",
            900,
            self.phase_event(
                "phase_started",
                "9",
                "retired-nine",
                1_000,
                phase_title="Retired investigation",
            ),
            self.phase_event(
                "phase_finished",
                "9",
                "retired-nine",
                1_166,
                phase_title="Retired investigation",
                status="stopped",
                phase_elapsed_seconds=166,
            ),
            self.phase_event(
                "phase_started",
                "9",
                "replacement-nine",
                2_000,
                phase_title="Replacement delivery",
            ),
            self.phase_event(
                "phase_finished",
                "9",
                "replacement-nine",
                6_040,
                phase_title="Replacement delivery",
                status="completed",
                phase_elapsed_seconds=4_040,
            ),
        )
        self.write_state(None)

        row = self.build(6_100).phases[0]

        self.assertIsInstance(row, phase_table.DonePhase)
        assert isinstance(row, phase_table.DonePhase)
        self.assertIsInstance(row.times, phase_table.Completed)
        assert isinstance(row.times, phase_table.Completed)
        self.assert_epoch(row.times.start, 2_000)
        self.assert_epoch(row.times.finish, 6_040)
        self.assertEqual(row.times.seconds, 4_040)

    def test_reworded_phase_heading_falls_back_to_recorded_id(self) -> None:
        self.write_plan(plan_text("### Phase 2 — Current wording  · status: done"))
        _ = self.write_run(
            "history",
            900,
            self.phase_event(
                "phase_started",
                "2",
                "two",
                1_000,
                phase_title="Original wording",
            ),
            self.phase_event(
                "phase_finished",
                "2",
                "two",
                1_120,
                phase_title="Original wording",
                status="completed",
                phase_elapsed_seconds=120,
            ),
        )
        self.write_state(None)

        row = self.build(1_200).phases[0]

        self.assertIsInstance(row, phase_table.DonePhase)
        assert isinstance(row, phase_table.DonePhase)
        self.assertIsInstance(row.times, phase_table.Completed)
        assert isinstance(row.times, phase_table.Completed)
        self.assert_epoch(row.times.start, 1_000)
        self.assert_epoch(row.times.finish, 1_120)

    def test_renumbered_phase_matches_normalized_recorded_title(self) -> None:
        self.write_plan(plan_text("### Phase 7 — Stable delivery  · status: done"))
        _ = self.write_run(
            "history",
            900,
            self.phase_event(
                "phase_started",
                "3",
                "three",
                1_000,
                phase_title="`STABLE   delivery`",
            ),
            self.phase_event(
                "phase_finished",
                "3",
                "three",
                1_180,
                phase_title="`STABLE   delivery`",
                status="completed",
                phase_elapsed_seconds=180,
            ),
        )
        self.write_state(None)

        row = self.build(1_200).phases[0]

        self.assertIsInstance(row, phase_table.DonePhase)
        assert isinstance(row, phase_table.DonePhase)
        self.assertIsInstance(row.times, phase_table.Completed)
        assert isinstance(row.times, phase_table.Completed)
        self.assert_epoch(row.times.start, 1_000)
        self.assert_epoch(row.times.finish, 1_180)
        self.assertEqual(row.times.seconds, 180)

    def test_todo_phase_ignores_recorded_stopped_attempt(self) -> None:
        self.write_plan(plan_text("### Phase 4 — Deferred delivery  · status: todo"))
        _ = self.write_run(
            "history",
            900,
            self.phase_event(
                "phase_started",
                "4",
                "four",
                1_000,
                phase_title="Deferred delivery",
            ),
            self.phase_event(
                "phase_finished",
                "4",
                "four",
                1_100,
                phase_title="Deferred delivery",
                status="stopped",
                phase_elapsed_seconds=100,
            ),
        )
        self.write_state(None)

        row = self.build(1_200).phases[0]

        self.assertIsInstance(row, phase_table.TodoPhase)
        assert isinstance(row, phase_table.TodoPhase)
        self.assertIsInstance(row.times, phase_table.NotPredicted)

    def test_gap_follows_written_phase_succession_not_plan_order(self) -> None:
        self.write_plan(
            plan_text(
                "### Phase 1 — First delivery  · status: done",
                "",
                "### Phase 2 — Second delivery  · status: done",
                "",
                "### Phase 3 — Current delivery  · status: todo",
                "",
                "### Phase 4 — Later delivery  · status: todo",
            )
        )
        _ = self.write_run(
            "history",
            900,
            self.phase_event(
                "phase_started", "2", "two", 1_000, phase_title="Second delivery"
            ),
            self.phase_event(
                "phase_finished",
                "2",
                "two",
                1_100,
                phase_title="Second delivery",
                status="completed",
                phase_elapsed_seconds=100,
            ),
            self.phase_event(
                "phase_started", "1", "one", 1_125, phase_title="First delivery"
            ),
            self.phase_event(
                "phase_finished",
                "1",
                "one",
                1_225,
                phase_title="First delivery",
                status="completed",
                phase_elapsed_seconds=100,
            ),
            self.phase_event(
                "phase_started",
                "3",
                "three",
                1_255,
                phase_title="Current delivery",
            ),
            self.phase_event(
                "progress_reported",
                "3",
                "three",
                1_305,
                phase_title="Current delivery",
                phase_percent=50,
                phase_elapsed_seconds=50,
                phase_calibration=None,
            ),
        )
        self.write_state(
            {
                "id": "3",
                "title": "Current delivery",
                "instance_id": "three",
                "started_at": 1_255,
                "status": "active",
            }
        )

        later = self.build(1_310).phases[3]

        self.assertIsInstance(later, phase_table.TodoPhase)
        assert isinstance(later, phase_table.TodoPhase)
        self.assertIsInstance(later.times, phase_table.Predicted)
        assert isinstance(later.times, phase_table.Predicted)
        self.assert_epoch(later.times.start, 1_382)
        self.assert_epoch(later.times.finish, 1_482)

    def test_later_written_report_wins_when_timestamps_match(self) -> None:
        self.write_plan(plan_text("### Phase 1 — Current delivery  · status: todo"))
        _ = self.write_run(
            "history",
            400,
            self.phase_event(
                "phase_started",
                "1",
                "older-one",
                500,
                phase_title="Current delivery",
            ),
            self.phase_event(
                "phase_finished",
                "1",
                "older-one",
                600,
                phase_title="Current delivery",
                status="stopped",
                phase_elapsed_seconds=100,
            ),
            self.phase_event(
                "phase_started",
                "1",
                "current-one",
                1_000,
                phase_title="Current delivery",
            ),
            self.phase_event(
                "progress_reported",
                "1",
                "current-one",
                1_100,
                phase_title="Current delivery",
                phase_percent=25,
                phase_elapsed_seconds=100,
                phase_calibration=None,
            ),
            self.phase_event(
                "progress_reported",
                "1",
                "current-one",
                1_100,
                phase_title="Current delivery",
                phase_percent=50,
                phase_elapsed_seconds=100,
                phase_calibration=None,
            ),
        )
        self.write_state(
            {
                "id": "1",
                "title": "Current delivery",
                "instance_id": "current-one",
                "started_at": 1_000,
                "status": "active",
            }
        )

        current = self.build(1_150).current

        self.assertIsInstance(current, phase_table.RunningPhase)
        assert isinstance(current, phase_table.RunningPhase)
        self.assert_epoch(current.started, 500)
        self.assertIsInstance(current.progress, phase_table.Reported)
        assert isinstance(current.progress, phase_table.Reported)
        self.assertEqual(current.progress.percent, 50)

    def test_active_phase_without_started_at_is_invalid_state(self) -> None:
        self.write_plan(plan_text("### Phase 1 — Current delivery  · status: todo"))
        self.write_state(
            {
                "id": "1",
                "title": "Current delivery",
                "instance_id": "one",
                "status": "active",
            }
        )

        with self.assertRaises(phase_table.NoState):
            _ = self.build(1_150)

    def test_active_phase_outside_plan_has_no_running_row(self) -> None:
        self.write_plan(plan_text("### Phase 1 — Planned delivery  · status: todo"))
        self.write_state(
            {
                "id": "ad-hoc",
                "title": "Unexpected repair",
                "instance_id": "repair",
                "started_at": 1_000,
                "status": "active",
            }
        )

        record = self.build(1_150)

        self.assertIsInstance(record.current, phase_table.RunningPhase)
        assert isinstance(record.current, phase_table.RunningPhase)
        self.assertEqual(record.current.title, "Unexpected repair")
        self.assertFalse(
            any(isinstance(row, phase_table.RunningPhase) for row in record.phases)
        )

    def test_render_is_exact_and_orders_newest_phase_first(self) -> None:
        zone = ZoneInfo("America/Los_Angeles")

        def epoch(hour: int, minute: int) -> int:
            return int(datetime(2026, 10, 8, hour, minute, tzinfo=zone).timestamp())

        self.write_plan(
            plan_text(
                "### Phase 1 — Archived delivery (`abc1234`)",
                "",
                "### Phase 2 — Finished delivery  · status: done",
                "",
                "### Phase 3 — Current delivery  · status: todo",
                "",
                "### Phase 4 — Later delivery  · status: todo",
            )
        )
        _ = self.write_run(
            "current",
            epoch(8, 0),
            self.phase_event("phase_started", "2", "two", epoch(8, 10)),
            self.phase_event(
                "phase_finished",
                "2",
                "two",
                epoch(9, 5),
                status="completed",
                phase_elapsed_seconds=3_300,
            ),
            self.phase_event("phase_started", "3", "three", epoch(9, 12)),
            self.phase_event(
                "progress_reported",
                "3",
                "three",
                epoch(9, 55),
                phase_percent=60,
                phase_elapsed_seconds=2_580,
                phase_calibration=None,
            ),
        )
        self.write_state(
            {
                "id": "3",
                "title": "Current delivery",
                "instance_id": "three",
                "started_at": epoch(9, 12),
                "status": "active",
            }
        )

        with patch.dict(
            os.environ,
            {
                "PLAN_DELEGATE_HISTORY_DIR": str(self.history_dir),
                "PLAN_DELEGATE_NOW_EPOCH": str(epoch(10, 0)),
            },
        ):
            rendered = phase_table.render(phase_table.build(self.session_dir), zone)

        self.assertEqual(
            rendered,
            "\n".join(
                (
                    "**Phase 3 of 4 — Current delivery**",
                    "",
                    "| | |",
                    "| --- | --- |",
                    "| Started | 10-08 09:12 |",
                    "| Done | 60% |",
                    "| ETA | 10-08 10:23 (10:13 to 10:38) |",
                    "| Plan finish | 10-08 11:25, predicted |",
                    "| Updated | 10-08 10:00 PDT |",
                    "",
                    "| Phase | What it delivers | Status | Start | Finish |",
                    "| --- | --- | --- | --- | --- |",
                    "| 4 | Later delivery | predicted | 10-08 10:30 | 10-08 11:25 |",
                    "| 3 | Current delivery | running, 60% | 10-08 09:12 | 10-08 10:23 |",
                    "| 2 | Finished delivery | done in 0:55 | 10-08 08:10 | 10-08 09:05 |",
                    "| 1 | Archived delivery | done | — | — |",
                )
            ),
        )

    def test_render_without_an_active_phase_omits_eta(self) -> None:
        self.write_plan(
            plan_text(
                "### Phase 1 — First delivery (`abc1234`)",
                "",
                "### Phase 2 — Second delivery  · status: done",
            )
        )
        _ = self.write_run("finished", 1_000)
        self.write_state(
            {
                "id": "2",
                "title": "Second delivery",
                "instance_id": "two",
                "started_at": 1_100,
                "status": "completed",
                "finished_at": 1_200,
            }
        )

        with patch.dict(
            os.environ,
            {
                "PLAN_DELEGATE_HISTORY_DIR": str(self.history_dir),
                "PLAN_DELEGATE_NOW_EPOCH": "1300",
            },
        ):
            rendered = phase_table.render(phase_table.build(self.session_dir), ZoneInfo("UTC"))

        self.assertTrue(rendered.startswith("**No phase running — 2 of 2 done**\n"))
        self.assertNotIn("| ETA |", rendered)

    def test_json_cli_uses_offset_times_and_the_public_shape(self) -> None:
        zone = ZoneInfo("America/Los_Angeles")
        started_at = int(datetime(2026, 10, 8, 9, 0, tzinfo=zone).timestamp())
        reported_at = started_at + 60
        self.write_plan(plan_text("### Phase 1 — Current delivery  · status: todo"))
        _ = self.write_run(
            "current",
            started_at - 60,
            self.phase_event("phase_started", "1", "one", started_at),
            self.phase_event(
                "progress_reported",
                "1",
                "one",
                reported_at,
                phase_percent=50,
                phase_elapsed_seconds=60,
                phase_calibration=None,
            ),
        )
        self.write_state(
            {
                "id": "1",
                "title": "Current delivery",
                "instance_id": "one",
                "started_at": started_at,
                "status": "active",
            }
        )
        environment = os.environ.copy()
        environment["PLAN_DELEGATE_HISTORY_DIR"] = str(self.history_dir)
        environment["PLAN_DELEGATE_NOW_EPOCH"] = str(reported_at)

        result = subprocess.run(
            [
                "python3",
                str(SCRIPT),
                "show",
                "--session-dir",
                str(self.session_dir),
                "--zone",
                "America/Los_Angeles",
                "--json",
            ],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        parsed = cast("dict[str, object]", json.loads(result.stdout))
        current = cast("dict[str, object]", parsed["current"])
        eta = cast("dict[str, object]", current["eta"])

        self.assertEqual(
            set(parsed),
            {"plan", "updated", "plan_finish", "current", "phases"},
        )
        self.assertTrue(cast(str, parsed["updated"]).endswith("-07:00"))
        self.assertTrue(cast(str, current["started"]).endswith("-07:00"))
        self.assertTrue(cast(str, eta["time"]).endswith("-07:00"))

    def test_cli_reports_missing_state_or_plan_on_one_line(self) -> None:
        empty_session = self.root / "empty-session"
        empty_session.mkdir()
        environment = os.environ.copy()
        environment["PLAN_DELEGATE_HISTORY_DIR"] = str(self.history_dir)

        result = subprocess.run(
            ["python3", str(SCRIPT), "show", "--session-dir", str(empty_session)],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )

        self.assertEqual(result.returncode, 1)
        self.assertEqual(len((result.stderr or result.stdout).splitlines()), 1)

        no_plan_session = self.root / "no-plan-session"
        no_plan_session.mkdir()
        _ = (no_plan_session / "progress_history_state.json").write_text(
            json.dumps(
                {
                    "working_dir": str(self.working_dir),
                    "plan_doc": "",
                    "project_plan_doc": "",
                    "phase": None,
                }
            ),
            encoding="utf-8",
        )
        result = subprocess.run(
            ["python3", str(SCRIPT), "show", "--session-dir", str(no_plan_session)],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )

        self.assertEqual(result.returncode, 1)
        self.assertEqual(len((result.stderr or result.stdout).splitlines()), 1)


if __name__ == "__main__":
    _ = unittest.main()
