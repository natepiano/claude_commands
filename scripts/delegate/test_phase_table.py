#!/usr/bin/env python3
"""Tests for the phase table built from delegate state and durable events."""

from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime
from pathlib import Path
from typing import cast, override
from unittest.mock import patch
from zoneinfo import ZoneInfo

from scripts.delegate import phase_table
from scripts.production import fake_showrunner, fake_tmux, showrunners


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
    vault_root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    tmux_state: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    sessions_dir: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    notifier_dir: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    live_record_number: int = 0
    showrunner_record_number: int = 0

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
        self.vault_root = self.root / "vault" / "showrunners"
        self.vault_root.parent.mkdir()
        self.tmux_state = self.root / "tmux.json"
        self.sessions_dir = self.root / "sessions"
        self.sessions_dir.mkdir()
        self.notifier_dir = self.root / "notifier"
        self.live_record_number = 0
        self.showrunner_record_number = 0
        _ = subprocess.run(
            ["git", "init", "-q", str(self.working_dir)],
            check=True,
            capture_output=True,
        )

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

    def environment(self, at: int = 1_200) -> dict[str, str]:
        return {
            **os.environ,
            "FAKE_TMUX_STATE": str(self.tmux_state),
            "NOTIFIER_STATE_DIR": str(self.notifier_dir),
            "NOTIFIER_SESSIONS_DIR": str(self.sessions_dir),
            "PHASE_TABLE_VAULT": str(self.vault_root),
            "PLAN_DELEGATE_HISTORY_DIR": str(self.history_dir),
            "PLAN_DELEGATE_NOW_EPOCH": str(at),
            "UNIT_LOOKUP_TMUX": str(
                Path(__file__).parents[1] / "production" / "fake_tmux.py"
            ),
        }

    def write_production(self) -> Path:
        path = self.working_dir / "docs" / "show-production.md"
        _ = path.write_text(
            plan_text(
                "# Production — show",
                "",
                "## Production Context",
                "",
                "- **Merge branch:** main",
                f"- **Showrunner checkout:** {self.working_dir}",
                "- **Log:** docs/show.log",
                "- **User zone:** UTC",
                "",
                "## Units",
            ),
            encoding="utf-8",
        )
        return path

    def write_showrunner(self, name: str = "showrunner") -> None:
        self.showrunner_record_number += 1
        session_id = f"showrunner-{self.showrunner_record_number}"
        production = self.working_dir / "docs" / "show-production.md"
        _ = fake_showrunner.write_timer(
            self.notifier_dir,
            "show",
            session_id,
            "UTC",
            production,
        )
        held = fake_showrunner.write_session(
            self.sessions_dir,
            name,
            session_id,
        )
        self.addCleanup(held.close)

    def write_stopped_showrunner(self) -> None:
        self.showrunner_record_number += 1
        production = self.working_dir / "docs" / "show-production.md"
        _ = fake_showrunner.write_timer(
            self.notifier_dir,
            "show",
            f"stopped-showrunner-{self.showrunner_record_number}",
            "UTC",
            production,
        )

    def write_production_plan(self, *, register_showrunner: bool = True) -> None:
        self.write_plan(
            plan_text(
                "# Delivery plan",
                "",
                "> **Production: show** — unit `table-unit`; production doc `docs/show-production.md`",
                "",
                "### Phase 1 — Current delivery  · status: todo",
            )
        )
        self.write_state(None)
        _ = self.write_run(
            "current",
            900,
            self.phase_event(
                "phase_started",
                "1",
                "one",
                1_000,
                phase_title="Current delivery",
            ),
        )
        _ = self.write_production()
        if register_showrunner:
            self.write_showrunner()

    def mark_unit(self, live_name: str = "") -> None:
        marked: fake_tmux.FakeSession = {
            "label": "table-tmux",
            "panes": ["%1"],
            "env": {
                "SHOWRUNNER_UNIT": "show",
                "SHOWRUNNER_UNIT_ID": "table-unit",
            },
        }
        fake_tmux.write(
            self.tmux_state,
            {"$1": marked},
        )
        if not live_name:
            return
        self.live_record_number += 1
        socket_path = self.root / f"live-{self.live_record_number}.sock"
        held = socket.socket(socket.AF_UNIX)
        held.bind(str(socket_path))
        self.addCleanup(held.close)
        _ = (self.sessions_dir / f"record-{self.live_record_number}.json").write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "sessionId": f"live-{self.live_record_number}",
                    "name": live_name,
                    "messagingSocketPath": str(socket_path),
                    "updatedAt": self.live_record_number,
                    "tmux": "table-tmux:@1.%1",
                }
            ),
            encoding="utf-8",
        )

    def run_refresh(self, at: int = 1_200) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "refresh",
                "--session-dir",
                str(self.session_dir),
            ],
            check=False,
            capture_output=True,
            text=True,
            env=self.environment(at),
        )

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
        self.assertIsInstance(current, phase_table.OpenPhase)
        assert isinstance(current, phase_table.OpenPhase)

        self.assertEqual(current.phase, "3")
        self.assert_epoch(current.started, 2_250)
        progress = current.progress
        self.assertIsInstance(progress, phase_table.ReportedPhaseProgress)
        assert isinstance(progress, phase_table.ReportedPhaseProgress)
        self.assertEqual(progress.percent, 50)
        projection = current.eta
        self.assertIsInstance(projection, phase_table.ProjectedEta)
        assert isinstance(projection, phase_table.ProjectedEta)
        self.assert_epoch(projection.time, 2_370)
        self.assert_epoch(projection.earliest, 2_350)
        self.assert_epoch(projection.latest, 2_400)
        self.assert_epoch(projection.as_of, 2_310)
        self.assertIsInstance(current.first_stated, phase_table.EtaNeverStated)

        archived = rows["1"]
        self.assertIsInstance(archived, phase_table.DonePhase)
        assert isinstance(archived, phase_table.DonePhase)
        self.assertIsInstance(archived.times, phase_table.PhaseTimingNotRecorded)
        repeated = rows["2"]
        self.assertIsInstance(repeated, phase_table.DonePhase)
        assert isinstance(repeated, phase_table.DonePhase)
        self.assertIsInstance(repeated.times, phase_table.CompletedPhaseTiming)
        assert isinstance(repeated.times, phase_table.CompletedPhaseTiming)
        self.assert_epoch(repeated.times.start, 1_100)
        self.assert_epoch(repeated.times.finish, 2_190)
        self.assertEqual(repeated.times.seconds, 300)
        self.assertIsInstance(rows["3"], phase_table.OpenPhase)
        later = rows["4"]
        self.assertIsInstance(later, phase_table.TodoPhase)
        assert isinstance(later, phase_table.TodoPhase)
        self.assertIsInstance(later.times, phase_table.PredictedPhaseTiming)
        assert isinstance(later.times, phase_table.PredictedPhaseTiming)
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
        self.assertIsInstance(later.times, phase_table.PredictedPhaseTiming)
        assert isinstance(later.times, phase_table.PredictedPhaseTiming)

        self.assert_epoch(later.times.start, 1_200)
        self.assert_epoch(later.times.finish, 1_400)
        self.assertIsInstance(record.plan_finish, phase_table.PredictedFinish)
        assert isinstance(record.plan_finish, phase_table.PredictedFinish)
        self.assert_epoch(record.plan_finish.at, 1_400)

    def test_stated_eta_wins_over_a_later_progress_report(self) -> None:
        self.write_plan(plan_text("### Phase 1 — Current delivery  · status: todo"))
        _ = self.write_run(
            "current",
            900,
            self.phase_event("phase_started", "1", "one", 1_000),
            self.phase_event(
                "eta_stated",
                "1",
                "one",
                1_050,
                eta_at=1_500,
                basis="three checks remain",
            ),
            self.phase_event(
                "progress_reported",
                "1",
                "one",
                1_100,
                phase_percent=60,
                phase_elapsed_seconds=100,
                phase_calibration=None,
            ),
        )
        self.write_state(None)

        current = self.build(1_200).current

        assert isinstance(current, phase_table.OpenPhase)
        assert isinstance(current.progress, phase_table.ReportedPhaseProgress)
        self.assertEqual(current.progress.percent, 60)
        self.assertIsInstance(current.eta, phase_table.StatedEta)
        assert isinstance(current.eta, phase_table.StatedEta)
        self.assert_epoch(current.eta.time, 1_500)
        self.assertIsInstance(current.eta.range, phase_table.NoEtaRange)
        self.assert_epoch(current.eta.stated_at, 1_050)
        self.assertEqual(current.eta.basis, "three checks remain")
        assert isinstance(current.first_stated, phase_table.FirstStatedEtaTarget)
        self.assert_epoch(current.first_stated.time, 1_500)

    def test_passed_stated_eta_yields_to_projection_and_keeps_first(self) -> None:
        self.write_plan(plan_text("### Phase 1 — Current delivery  · status: todo"))
        _ = self.write_run(
            "current",
            900,
            self.phase_event("phase_started", "1", "one", 1_000),
            self.phase_event(
                "eta_stated",
                "1",
                "one",
                1_050,
                eta_at=1_150,
                basis="initial target",
            ),
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
        self.write_state(None)

        current = self.build(1_160).current

        assert isinstance(current, phase_table.OpenPhase)
        self.assertIsInstance(current.eta, phase_table.ProjectedEta)
        assert isinstance(current.eta, phase_table.ProjectedEta)
        self.assert_epoch(current.eta.time, 1_200)
        assert isinstance(current.first_stated, phase_table.FirstStatedEtaTarget)
        self.assert_epoch(current.first_stated.time, 1_150)

    def test_first_stated_target_holds_across_restatement(self) -> None:
        self.write_plan(plan_text("### Phase 1 — Current delivery  · status: todo"))
        _ = self.write_run(
            "current",
            900,
            self.phase_event("phase_started", "1", "one", 1_000),
            self.phase_event(
                "eta_stated",
                "1",
                "one",
                1_050,
                eta_at=1_400,
                basis="first target",
            ),
            self.phase_event(
                "eta_stated",
                "1",
                "one",
                1_100,
                eta_at=1_600,
                eta_earliest_at=1_500,
                eta_latest_at=1_700,
                basis="expanded verification",
            ),
        )
        self.write_state(None)

        current = self.build(1_200).current

        assert isinstance(current, phase_table.OpenPhase)
        assert isinstance(current.eta, phase_table.StatedEta)
        self.assert_epoch(current.eta.time, 1_600)
        assert isinstance(current.eta.range, phase_table.EtaRange)
        self.assert_epoch(current.eta.range.earliest, 1_500)
        self.assert_epoch(current.eta.range.latest, 1_700)
        assert isinstance(current.first_stated, phase_table.FirstStatedEtaTarget)
        self.assert_epoch(current.first_stated.time, 1_400)

    def test_new_phase_does_not_inherit_the_previous_stated_eta(self) -> None:
        self.write_plan(
            plan_text(
                "### Phase 1 — Earlier delivery  · status: done",
                "",
                "### Phase 2 — Current delivery  · status: todo",
            )
        )
        _ = self.write_run(
            "current",
            800,
            self.phase_event("phase_started", "1", "one", 900),
            self.phase_event(
                "eta_stated",
                "1",
                "one",
                950,
                eta_at=1_100,
                basis="earlier phase",
            ),
            self.phase_event(
                "phase_finished",
                "1",
                "one",
                1_000,
                status="completed",
                phase_elapsed_seconds=100,
            ),
            self.phase_event("phase_started", "2", "two", 1_050),
        )
        self.write_state(None)

        current = self.build(1_100).current

        assert isinstance(current, phase_table.OpenPhase)
        self.assertEqual(current.phase, "2")
        self.assertIsInstance(current.eta, phase_table.EtaUnavailable)
        self.assertIsInstance(current.first_stated, phase_table.EtaNeverStated)

    def test_stated_eta_without_progress_drives_later_predictions(self) -> None:
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
                "eta_stated",
                "1",
                "one",
                1_050,
                eta_at=1_300,
                basis="remaining test matrix",
            ),
        )
        self.write_state(None)

        record = self.build(1_100)
        current = record.current
        later = record.phases[1]

        assert isinstance(current, phase_table.OpenPhase)
        self.assertIsInstance(current.progress, phase_table.PhaseProgressNotReported)
        self.assertIsInstance(current.eta, phase_table.StatedEta)
        assert isinstance(later, phase_table.TodoPhase)
        assert isinstance(later.times, phase_table.PredictedPhaseTiming)
        self.assert_epoch(later.times.start, 1_300)
        self.assert_epoch(later.times.finish, 1_600)

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
        self.assertIsInstance(current, phase_table.OpenPhase)
        assert isinstance(current, phase_table.OpenPhase)
        self.assertIsInstance(current.progress, phase_table.PhaseProgressNotReported)
        later = record.phases[1]
        self.assertIsInstance(later, phase_table.TodoPhase)
        assert isinstance(later, phase_table.TodoPhase)
        self.assertIsInstance(later.times, phase_table.PhaseTimingNotPredicted)
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
        self.assertIsInstance(current, phase_table.OpenPhase)
        assert isinstance(current, phase_table.OpenPhase)
        self.assertIsInstance(current.progress, phase_table.ReportedPhaseProgress)
        assert isinstance(current.progress, phase_table.ReportedPhaseProgress)
        self.assertEqual(current.progress.percent, 0)
        self.assertIsInstance(current.eta, phase_table.EtaUnavailable)
        later = record.phases[1]
        self.assertIsInstance(later, phase_table.TodoPhase)
        assert isinstance(later, phase_table.TodoPhase)
        self.assertIsInstance(later.times, phase_table.PhaseTimingNotPredicted)

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
        self.assertIsInstance(row.times, phase_table.CompletedPhaseTiming)
        assert isinstance(row.times, phase_table.CompletedPhaseTiming)
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
        self.assertIsInstance(row.times, phase_table.CompletedPhaseTiming)
        assert isinstance(row.times, phase_table.CompletedPhaseTiming)
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
        self.assertIsInstance(row.times, phase_table.CompletedPhaseTiming)
        assert isinstance(row.times, phase_table.CompletedPhaseTiming)
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
        self.assertIsInstance(row.times, phase_table.PhaseTimingNotPredicted)

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
        self.assertIsInstance(later.times, phase_table.PredictedPhaseTiming)
        assert isinstance(later.times, phase_table.PredictedPhaseTiming)
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

        self.assertIsInstance(current, phase_table.OpenPhase)
        assert isinstance(current, phase_table.OpenPhase)
        self.assert_epoch(current.started, 500)
        self.assertIsInstance(current.progress, phase_table.ReportedPhaseProgress)
        assert isinstance(current.progress, phase_table.ReportedPhaseProgress)
        self.assertEqual(current.progress.percent, 50)

    def test_phase_in_state_without_start_event_is_not_open(self) -> None:
        self.write_plan(plan_text("### Phase 1 — Current delivery  · status: todo"))
        self.write_state(
            {
                "id": "1",
                "title": "Current delivery",
                "instance_id": "one",
                "status": "active",
            }
        )

        record = self.build(1_150)

        self.assertIsInstance(record.current, phase_table.NoOpenPhase)
        self.assertFalse(
            any(isinstance(row, phase_table.OpenPhase) for row in record.phases)
        )

    def test_active_phase_outside_plan_has_no_running_row(self) -> None:
        self.write_plan(plan_text("### Phase 1 — Planned delivery  · status: todo"))
        _ = self.write_run(
            "current",
            900,
            self.phase_event(
                "phase_started",
                "ad-hoc",
                "repair",
                1_000,
                phase_title="Unexpected repair",
            ),
        )
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

        self.assertIsInstance(record.current, phase_table.OpenPhase)
        assert isinstance(record.current, phase_table.OpenPhase)
        self.assertEqual(record.current.title, "Unexpected repair")
        self.assertFalse(
            any(isinstance(row, phase_table.OpenPhase) for row in record.phases)
        )

    def test_stopped_phase_is_not_open(self) -> None:
        self.write_plan(plan_text("### Phase 1 — Stopped delivery  · status: todo"))
        _ = self.write_run(
            "current",
            900,
            self.phase_event(
                "phase_started", "1", "one", 1_000, phase_title="Stopped delivery"
            ),
            self.phase_event(
                "phase_finished",
                "1",
                "one",
                1_100,
                status="stopped",
                phase_elapsed_seconds=100,
            ),
        )
        self.write_state(None)

        self.assertIsInstance(self.build(1_200).current, phase_table.NoOpenPhase)

    def test_run_finished_closes_an_unfinished_phase_for_the_table(self) -> None:
        self.write_plan(plan_text("### Phase 1 — Finished run  · status: todo"))
        _ = self.write_run(
            "current",
            900,
            self.phase_event(
                "phase_started", "1", "one", 1_000, phase_title="Finished run"
            ),
            {"event_type": "run_finished", "timestamp_epoch": 1_100},
        )
        self.write_state(None)

        self.assertIsInstance(self.build(1_200).current, phase_table.NoOpenPhase)

    def test_unfinished_phase_in_older_run_is_not_open(self) -> None:
        self.write_plan(plan_text("### Phase 1 — Old delivery  · status: todo"))
        _ = self.write_run(
            "older",
            900,
            self.phase_event(
                "phase_started", "1", "old", 1_000, phase_title="Old delivery"
            ),
        )
        _ = self.write_run("newer", 1_100)
        self.write_state(None)

        self.assertIsInstance(self.build(1_200).current, phase_table.NoOpenPhase)

    def test_run_with_unparseable_first_line_is_skipped(self) -> None:
        self.write_plan(plan_text("### Phase 1 — Ignored delivery  · status: todo"))
        broken = self.history_dir / "runs" / "broken.jsonl"
        broken.parent.mkdir(parents=True)
        _ = broken.write_text(
            "not json\n"
            + json.dumps(
                self.phase_event(
                    "phase_started",
                    "1",
                    "ignored",
                    1_000,
                    phase_title="Ignored delivery",
                )
            )
            + "\n",
            encoding="utf-8",
        )
        self.write_state(None)

        self.assertIsInstance(self.build(1_200).current, phase_table.NoOpenPhase)

    def test_unreported_open_phase_predictions_use_later_of_typical_finish_and_now(
        self,
    ) -> None:
        self.write_plan(
            plan_text(
                "### Phase 1 — Prior delivery  · status: done",
                "",
                "### Phase 2 — Current delivery  · status: todo",
                "",
                "### Phase 3 — Later delivery  · status: todo",
            )
        )
        _ = self.write_run(
            "older",
            700,
            self.phase_event(
                "phase_started", "1", "old", 800, phase_title="Prior delivery"
            ),
            self.phase_event(
                "phase_finished",
                "1",
                "old",
                900,
                status="completed",
                phase_elapsed_seconds=100,
            ),
        )
        _ = self.write_run(
            "current",
            950,
            self.phase_event(
                "phase_started",
                "2",
                "current",
                1_000,
                phase_title="Current delivery",
            ),
        )
        self.write_state(None)

        before_now = self.build(1_050).phases[2]
        after_typical = self.build(1_200).phases[2]

        assert isinstance(before_now, phase_table.TodoPhase)
        assert isinstance(before_now.times, phase_table.PredictedPhaseTiming)
        self.assert_epoch(before_now.times.start, 1_100)
        assert isinstance(after_typical, phase_table.TodoPhase)
        assert isinstance(after_typical.times, phase_table.PredictedPhaseTiming)
        self.assert_epoch(after_typical.times.start, 1_200)

    def test_no_open_phase_predictions_start_now(self) -> None:
        self.write_plan(
            plan_text(
                "### Phase 1 — Prior delivery  · status: done",
                "",
                "### Phase 2 — Next delivery  · status: todo",
            )
        )
        _ = self.write_run(
            "finished",
            700,
            self.phase_event(
                "phase_started", "1", "one", 800, phase_title="Prior delivery"
            ),
            self.phase_event(
                "phase_finished",
                "1",
                "one",
                900,
                status="completed",
                phase_elapsed_seconds=100,
            ),
            {"event_type": "run_finished", "timestamp_epoch": 910},
        )
        self.write_state(None)

        row = self.build(1_200).phases[1]

        assert isinstance(row, phase_table.TodoPhase)
        assert isinstance(row.times, phase_table.PredictedPhaseTiming)
        self.assert_epoch(row.times.start, 1_200)

    def test_done_row_after_todo_does_not_make_unfinished_plan_finished(self) -> None:
        self.write_plan(
            plan_text(
                "### Phase 1 — Next delivery  · status: todo",
                "",
                "### Phase 2 — Earlier delivery  · status: done",
            )
        )
        _ = self.write_run(
            "finished",
            700,
            self.phase_event(
                "phase_started", "2", "two", 800, phase_title="Earlier delivery"
            ),
            self.phase_event(
                "phase_finished",
                "2",
                "two",
                900,
                status="completed",
                phase_elapsed_seconds=100,
            ),
            {"event_type": "run_finished", "timestamp_epoch": 910},
        )
        self.write_state(None)

        record = self.build(1_200)
        todo = record.phases[0]

        assert isinstance(todo, phase_table.TodoPhase)
        assert isinstance(todo.times, phase_table.PredictedPhaseTiming)
        self.assert_epoch(todo.times.finish, 1_300)
        assert isinstance(record.plan_finish, phase_table.PredictedFinish)
        self.assert_epoch(record.plan_finish.at, 1_300)

    def test_todo_before_open_phase_is_predicted_after_open_eta(self) -> None:
        self.write_plan(
            plan_text(
                "### Phase 1 — Follow-up delivery  · status: todo",
                "",
                "### Phase 2 — Current delivery  · status: todo",
            )
        )
        _ = self.write_run(
            "current",
            900,
            self.phase_event(
                "phase_started",
                "2",
                "current",
                1_000,
                phase_title="Current delivery",
            ),
            self.phase_event(
                "progress_reported",
                "2",
                "current",
                1_100,
                phase_percent=50,
                phase_elapsed_seconds=100,
                phase_calibration=None,
            ),
        )
        self.write_state(None)

        record = self.build(1_150)
        follow_up = record.phases[0]

        assert isinstance(follow_up, phase_table.TodoPhase)
        assert isinstance(follow_up.times, phase_table.PredictedPhaseTiming)
        self.assert_epoch(follow_up.times.start, 1_200)
        self.assert_epoch(follow_up.times.finish, 1_400)
        assert isinstance(record.plan_finish, phase_table.PredictedFinish)
        self.assert_epoch(record.plan_finish.at, 1_400)

    def test_all_done_plan_uses_latest_finish_across_document_order(self) -> None:
        self.write_plan(
            plan_text(
                "### Phase 1 — Later finish  · status: done",
                "",
                "### Phase 2 — Earlier finish  · status: done",
            )
        )
        _ = self.write_run(
            "finished",
            700,
            self.phase_event(
                "phase_started", "1", "one", 800, phase_title="Later finish"
            ),
            self.phase_event(
                "phase_finished",
                "1",
                "one",
                2_000,
                status="completed",
                phase_elapsed_seconds=1_200,
            ),
            self.phase_event(
                "phase_started", "2", "two", 1_000, phase_title="Earlier finish"
            ),
            self.phase_event(
                "phase_finished",
                "2",
                "two",
                1_500,
                status="completed",
                phase_elapsed_seconds=500,
            ),
            {"event_type": "run_finished", "timestamp_epoch": 2_010},
        )
        self.write_state(None)

        finish = self.build(2_100).plan_finish

        assert isinstance(finish, phase_table.FinishedAt)
        self.assert_epoch(finish.at, 2_000)

    def test_title_matched_open_phase_uses_current_plan_number(self) -> None:
        self.write_plan(plan_text("### Phase 7 — Stable delivery  · status: todo"))
        _ = self.write_run(
            "current",
            900,
            self.phase_event(
                "phase_started",
                "3",
                "renumbered",
                1_000,
                phase_title="Stable delivery",
            ),
        )
        self.write_state(None)

        record = self.build(1_100)

        assert isinstance(record.current, phase_table.OpenPhase)
        self.assertEqual(record.current.phase, "7")
        row = record.phases[0]
        assert isinstance(row, phase_table.OpenPhase)
        self.assertEqual(row.phase, "7")

    def test_build_plan_needs_no_session_state(self) -> None:
        self.write_plan(plan_text("### Phase 1 — Delivery  · status: todo"))

        with patch.dict(
            os.environ,
            {
                "PLAN_DELEGATE_HISTORY_DIR": str(self.history_dir),
                "PLAN_DELEGATE_NOW_EPOCH": "1200",
            },
        ):
            record = phase_table.build_plan(self.plan)

        self.assertEqual(record.plan, self.plan.resolve())
        self.assertIsInstance(record.current, phase_table.NoOpenPhase)

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
                    "| ETA from | projected from 60% done |",
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

    def test_render_exact_stated_eta_and_json_shape(self) -> None:
        zone = ZoneInfo("America/Los_Angeles")

        def epoch(hour: int, minute: int) -> int:
            return int(datetime(2026, 10, 8, hour, minute, tzinfo=zone).timestamp())

        self.write_plan(plan_text("### Phase 1 — Current delivery  · status: todo"))
        _ = self.write_run(
            "current",
            epoch(9, 0),
            self.phase_event("phase_started", "1", "one", epoch(9, 10)),
            self.phase_event(
                "eta_stated",
                "1",
                "one",
                epoch(9, 50),
                eta_at=epoch(10, 30),
                basis="two checks remain",
            ),
        )
        self.write_state(None)

        with patch.dict(
            os.environ,
            {
                "PLAN_DELEGATE_HISTORY_DIR": str(self.history_dir),
                "PLAN_DELEGATE_NOW_EPOCH": str(epoch(10, 0)),
            },
        ):
            rendered = phase_table.show(self.session_dir, zone)
            parsed = cast(
                "dict[str, object]",
                json.loads(phase_table.show(self.session_dir, zone, json_output=True)),
            )

        self.assertIn("| ETA | 10-08 10:30 |", rendered)
        self.assertIn("| ETA from | stated 09:50: two checks remain |", rendered)
        current_json = cast("dict[str, object]", parsed["current"])
        eta_json = cast("dict[str, object]", current_json["eta"])
        self.assertEqual(
            set(eta_json),
            {
                "time",
                "earliest",
                "latest",
                "source",
                "stated_at",
                "basis",
                "as_of",
                "first",
            },
        )
        self.assertEqual(eta_json["source"], "stated")
        self.assertIsNone(eta_json["earliest"])
        self.assertIsNone(eta_json["latest"])
        self.assertIsNone(eta_json["as_of"])
        self.assertEqual(eta_json["basis"], "two checks remain")
        self.assertEqual(eta_json["time"], eta_json["first"])
        self.assertTrue(cast(str, eta_json["stated_at"]).endswith("-07:00"))

    def test_render_ranged_stated_eta(self) -> None:
        zone = ZoneInfo("America/Los_Angeles")

        def epoch(hour: int, minute: int) -> int:
            return int(datetime(2026, 10, 8, hour, minute, tzinfo=zone).timestamp())

        self.write_plan(plan_text("### Phase 1 — Current delivery  · status: todo"))
        _ = self.write_run(
            "current",
            epoch(9, 0),
            self.phase_event("phase_started", "1", "one", epoch(9, 10)),
            self.phase_event(
                "eta_stated",
                "1",
                "one",
                epoch(9, 50),
                eta_at=epoch(10, 30),
                eta_earliest_at=epoch(10, 15),
                eta_latest_at=epoch(10, 45),
                basis="integration range",
            ),
        )
        self.write_state(None)

        with patch.dict(
            os.environ,
            {
                "PLAN_DELEGATE_HISTORY_DIR": str(self.history_dir),
                "PLAN_DELEGATE_NOW_EPOCH": str(epoch(10, 0)),
            },
        ):
            rendered = phase_table.show(self.session_dir, zone)

        self.assertIn("| ETA | 10-08 10:30 (10:15 to 10:45) |", rendered)
        self.assertIn("| ETA from | stated 09:50: integration range |", rendered)

    def test_render_keeps_a_stated_basis_inside_its_table_cell(self) -> None:
        zone = ZoneInfo("America/Los_Angeles")
        basis = "lint | full tests\nthen the live check"

        def epoch(hour: int, minute: int) -> int:
            return int(datetime(2026, 10, 8, hour, minute, tzinfo=zone).timestamp())

        self.write_plan(plan_text("### Phase 1 — Current delivery  · status: todo"))
        _ = self.write_run(
            "current",
            epoch(9, 0),
            self.phase_event("phase_started", "1", "one", epoch(9, 10)),
            self.phase_event(
                "eta_stated",
                "1",
                "one",
                epoch(9, 50),
                eta_at=epoch(10, 30),
                basis=basis,
            ),
        )
        self.write_state(None)

        with patch.dict(
            os.environ,
            {
                "PLAN_DELEGATE_HISTORY_DIR": str(self.history_dir),
                "PLAN_DELEGATE_NOW_EPOCH": str(epoch(10, 0)),
            },
        ):
            rendered = phase_table.show(self.session_dir, zone)
            parsed = cast(
                "dict[str, object]",
                json.loads(phase_table.show(self.session_dir, zone, json_output=True)),
            )

        self.assertIn(
            "| ETA from | stated 09:50: lint \\| full tests then the live check |",
            rendered,
        )
        current_json = cast("dict[str, object]", parsed["current"])
        eta_json = cast("dict[str, object]", current_json["eta"])
        self.assertEqual(eta_json["basis"], basis)

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
        self.assertEqual(
            set(eta),
            {
                "time",
                "earliest",
                "latest",
                "source",
                "stated_at",
                "basis",
                "as_of",
                "first",
            },
        )
        self.assertEqual(eta["source"], "projected")
        self.assertIsNone(eta["stated_at"])
        self.assertIsNone(eta["basis"])
        self.assertTrue(cast(str, eta["as_of"]).endswith("-07:00"))
        self.assertIsNone(eta["first"])

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

    def test_refresh_writes_exact_note_bytes_under_live_session_name(self) -> None:
        self.write_production_plan()
        self.mark_unit(".live/name")
        history_before = {
            path: path.read_bytes() for path in self.history_dir.rglob("*") if path.is_file()
        }

        result = self.run_refresh()

        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        note = self.vault_root / "showrunner" / "live-name.md"
        self.assertEqual(
            note.read_text(encoding="utf-8"),
            "\n".join(
                (
                    "---",
                    "phase_table: true",
                    "production: show",
                    "unit: table-unit",
                    "---",
                    "",
                    "# live-name",
                    "",
                    "**Phase 1 of 1 — Current delivery**",
                    "",
                    "| | |",
                    "| --- | --- |",
                    "| Started | 01-01 00:16 |",
                    "| Done | — |",
                    "| ETA | — |",
                    "| ETA from | — |",
                    "| Plan finish | — |",
                    "| Updated | 01-01 00:20 UTC |",
                    "",
                    "| Phase | What it delivers | Status | Start | Finish |",
                    "| --- | --- | --- | --- | --- |",
                    "| 1 | Current delivery | running | 01-01 00:16 | — |",
                    "",
                )
            ),
        )
        self.assertEqual(note.stat().st_mode & 0o777, 0o644)
        self.assertEqual(
            [path.relative_to(self.vault_root) for path in self.vault_root.rglob("*") if path.is_file()],
            [Path("showrunner/live-name.md")],
        )
        self.assertEqual(
            {
                path: path.read_bytes()
                for path in self.history_dir.rglob("*")
                if path.is_file()
            },
            history_before,
        )

    def test_refresh_uses_unit_id_without_a_live_claude(self) -> None:
        self.write_production_plan()
        self.mark_unit()

        result = self.run_refresh()

        self.assertEqual(result.returncode, 0)
        self.assertTrue(
            (self.vault_root / "showrunner" / "table-unit.md").is_file()
        )

    def test_renamed_live_session_moves_its_note(self) -> None:
        self.write_production_plan()
        self.mark_unit("before")
        self.assertEqual(self.run_refresh().returncode, 0)
        self.mark_unit("after")

        result = self.run_refresh()

        folder = self.vault_root / "showrunner"
        self.assertEqual(result.returncode, 0)
        self.assertFalse((folder / "before.md").exists())
        self.assertTrue((folder / "after.md").is_file())

    def test_changed_showrunner_moves_note_and_removes_empty_directory(self) -> None:
        self.write_production_plan()
        self.mark_unit("table")
        self.assertEqual(self.run_refresh().returncode, 0)
        self.write_showrunner("new-showrunner")

        result = self.run_refresh()

        self.assertEqual(result.returncode, 0)
        self.assertFalse((self.vault_root / "showrunner").exists())
        self.assertTrue(
            (self.vault_root / "new-showrunner" / "table.md").is_file()
        )

    def test_refresh_leaves_handwritten_note_in_same_folder(self) -> None:
        self.write_production_plan()
        self.mark_unit("table")
        folder = self.vault_root / "showrunner"
        folder.mkdir(parents=True)
        handwritten = folder / "notes.md"
        _ = handwritten.write_text("kept by a person\n", encoding="utf-8")

        result = self.run_refresh()

        self.assertEqual(result.returncode, 0)
        self.assertEqual(handwritten.read_text(encoding="utf-8"), "kept by a person\n")

    def test_refresh_refuses_to_replace_handwritten_target(self) -> None:
        self.write_production_plan()
        self.mark_unit("table")
        target = self.vault_root / "showrunner" / "table.md"
        target.parent.mkdir(parents=True)
        _ = target.write_text("personal note\n", encoding="utf-8")

        result = self.run_refresh()

        self.assertEqual(result.returncode, 1)
        self.assertIn("not owned", result.stderr)
        self.assertEqual(target.read_text(encoding="utf-8"), "personal note\n")

    def test_refresh_refuses_to_replace_another_units_note(self) -> None:
        self.write_production_plan()
        self.mark_unit("table")
        target = self.vault_root / "showrunner" / "table.md"
        target.parent.mkdir(parents=True)
        other = "---\nphase_table: true\nproduction: show\nunit: other\n---\n"
        _ = target.write_text(other, encoding="utf-8")

        result = self.run_refresh()

        self.assertEqual(result.returncode, 1)
        self.assertEqual(target.read_text(encoding="utf-8"), other)

    def test_refresh_refuses_file_that_appears_during_publication(self) -> None:
        self.write_production_plan()
        self.mark_unit("table")
        target = self.vault_root / "showrunner" / "table.md"
        competing = b"created by another refresh\n"

        def create_competing_note(_descriptor: int, _mode: int) -> None:
            _ = target.write_bytes(competing)

        errors = io.StringIO()
        arguments = [
            str(SCRIPT),
            "refresh",
            "--session-dir",
            str(self.session_dir),
        ]
        with (
            patch.dict(os.environ, self.environment()),
            patch.object(showrunners, "NOTIFIER_STATE_DIR", self.notifier_dir),
            patch.object(showrunners, "SESSIONS_DIR", self.sessions_dir),
            patch.object(os, "fchmod", side_effect=create_competing_note),
            patch.object(sys, "argv", arguments),
            redirect_stderr(errors),
        ):
            result = phase_table.main()

        self.assertEqual(result, 1)
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertIn(str(target), errors.getvalue())
        self.assertEqual(target.read_bytes(), competing)
        self.assertEqual(list(target.parent.iterdir()), [target])

    def test_refresh_refuses_when_showrunner_folder_is_a_file(self) -> None:
        self.write_production_plan()
        blocked_folder = self.vault_root / "showrunner"
        blocked_folder.parent.mkdir(parents=True)
        _ = blocked_folder.write_text("not a folder\n", encoding="utf-8")

        result = self.run_refresh()

        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("cannot write phase note", result.stderr)

    def test_dot_showrunner_names_are_refused_without_writing(self) -> None:
        self.write_production_plan()

        for showrunner in (".", ".."):
            with self.subTest(showrunner=showrunner):
                self.write_showrunner(showrunner)
                result = self.run_refresh()

                self.assertEqual(result.returncode, 1)
                self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertFalse(self.vault_root.exists())
        self.assertEqual(list(self.vault_root.parent.iterdir()), [])

    def test_stopped_showrunner_uses_newest_owned_note_folder(self) -> None:
        self.write_production_plan()
        self.mark_unit("before")
        self.assertEqual(self.run_refresh().returncode, 0)
        original = self.vault_root / "showrunner" / "before.md"
        newer_folder = self.vault_root / "last-known-showrunner"
        newer_folder.mkdir()
        newer = newer_folder / "before.md"
        _ = newer.write_bytes(original.read_bytes())
        os.utime(original, ns=(100, 100))
        os.utime(newer, ns=(200, 200))
        self.write_stopped_showrunner()
        self.mark_unit("after")

        result = self.run_refresh(at=1_300)

        target = newer_folder / "after.md"
        self.assertEqual(result.returncode, 0)
        self.assertTrue(target.is_file())
        self.assertIn("| Updated | 01-01 00:21 UTC |", target.read_text(encoding="utf-8"))
        self.assertEqual(
            [path.relative_to(self.vault_root) for path in self.vault_root.rglob("*.md")],
            [Path("last-known-showrunner/after.md")],
        )

    def test_stopped_showrunner_without_owned_note_is_refused_before_writing(
        self,
    ) -> None:
        self.write_production_plan(register_showrunner=False)
        self.write_stopped_showrunner()
        self.vault_root = self.working_dir / "showrunners"
        exclude = self.working_dir / ".git" / "info" / "exclude"
        exclude_before = exclude.read_bytes()

        result = self.run_refresh()

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr.strip(), "the showrunner of show is not running")
        self.assertFalse(self.vault_root.exists())
        self.assertEqual(exclude.read_bytes(), exclude_before)

    def test_production_without_update_timer_is_refused_before_writing(self) -> None:
        self.write_production_plan(register_showrunner=False)
        self.vault_root = self.working_dir / "showrunners"
        exclude = self.working_dir / ".git" / "info" / "exclude"
        exclude_before = exclude.read_bytes()

        result = self.run_refresh()

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr.strip(), "the showrunner of show is not running")
        self.assertFalse(self.vault_root.exists())
        self.assertEqual(exclude.read_bytes(), exclude_before)

    def test_showrunner_lookup_error_is_refused_before_writing(self) -> None:
        self.write_production_plan(register_showrunner=False)
        self.vault_root = self.working_dir / "showrunners"
        exclude = self.working_dir / ".git" / "info" / "exclude"
        exclude_before = exclude.read_bytes()
        errors = io.StringIO()
        arguments = [
            str(SCRIPT),
            "refresh",
            "--session-dir",
            str(self.session_dir),
        ]

        with (
            patch.dict(os.environ, self.environment()),
            patch.object(showrunners, "NOTIFIER_STATE_DIR", self.notifier_dir),
            patch.object(showrunners, "SESSIONS_DIR", self.sessions_dir),
            patch.object(
                showrunners,
                "current_name",
                side_effect=OSError("session records unavailable"),
            ),
            patch.object(sys, "argv", arguments),
            redirect_stderr(errors),
        ):
            result = phase_table.main()

        self.assertEqual(result, 1)
        self.assertEqual(
            errors.getvalue().strip(),
            "cannot look up the showrunner of show: session records unavailable",
        )
        self.assertFalse(self.vault_root.exists())
        self.assertEqual(exclude.read_bytes(), exclude_before)

    def test_plan_without_production_line_writes_nothing(self) -> None:
        self.write_plan(plan_text("### Phase 1 — Local delivery  · status: todo"))
        self.write_state(None)

        result = self.run_refresh()

        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertFalse(self.vault_root.exists())

    def test_missing_vault_parent_writes_nothing(self) -> None:
        self.write_production_plan()
        self.vault_root = self.root / "absent" / "showrunners"

        result = self.run_refresh()

        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertFalse(self.vault_root.exists())

    def test_invalid_production_document_is_one_line_refusal(self) -> None:
        self.write_production_plan()
        production = self.working_dir / "docs" / "show-production.md"
        _ = production.write_text("# Incomplete production\n", encoding="utf-8")

        result = self.run_refresh()

        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(result.stderr.splitlines()), 1)

    def test_refresh_adds_showrunners_to_containing_checkout_exclude_once(self) -> None:
        self.write_production_plan()
        self.mark_unit("table")
        self.vault_root = self.working_dir / "showrunners"

        first = self.run_refresh()
        second = self.run_refresh()

        exclude = self.working_dir / ".git" / "info" / "exclude"
        self.assertEqual((first.returncode, second.returncode), (0, 0))
        self.assertEqual(exclude.read_text(encoding="utf-8").splitlines().count("showrunners/"), 1)
        self.assertFalse((self.vault_root / ".gitignore").exists())

    def test_refresh_excludes_vaults_relative_path_once(self) -> None:
        self.write_production_plan()
        self.mark_unit("table")
        self.vault_root = self.working_dir / "notes" / "tables"
        self.vault_root.parent.mkdir()

        first = self.run_refresh()
        second = self.run_refresh()

        exclude = self.working_dir / ".git" / "info" / "exclude"
        lines = exclude.read_text(encoding="utf-8").splitlines()
        self.assertEqual((first.returncode, second.returncode), (0, 0))
        self.assertEqual(lines.count("notes/tables/"), 1)
        self.assertNotIn("showrunners/", lines)

        before_top_level_refresh = exclude.read_bytes()
        self.vault_root = self.working_dir
        top_level = self.run_refresh()

        self.assertEqual(top_level.returncode, 0)
        self.assertEqual(exclude.read_bytes(), before_top_level_refresh)

    def test_refresh_reports_missing_state_and_unreadable_plan(self) -> None:
        empty_session = self.root / "missing-state"
        empty_session.mkdir()
        missing_state = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "refresh",
                "--session-dir",
                str(empty_session),
            ],
            check=False,
            capture_output=True,
            text=True,
            env=self.environment(),
        )
        missing_plan = self.working_dir / "docs" / "missing.md"
        self.write_state(None)
        state = cast(
            "dict[str, object]",
            json.loads(
                (self.session_dir / "progress_history_state.json").read_text(
                    encoding="utf-8"
                )
            ),
        )
        state["project_plan_doc"] = str(missing_plan)
        _ = (self.session_dir / "progress_history_state.json").write_text(
            json.dumps(state), encoding="utf-8"
        )

        unreadable_plan = self.run_refresh()
        unreadable_show = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "show",
                "--session-dir",
                str(self.session_dir),
            ],
            check=False,
            capture_output=True,
            text=True,
            env=self.environment(),
        )

        self.assertEqual(missing_state.returncode, 1)
        self.assertEqual(len(missing_state.stderr.splitlines()), 1)
        self.assertEqual(unreadable_plan.returncode, 1)
        self.assertIn(str(missing_plan), unreadable_plan.stderr)
        self.assertEqual(unreadable_show.returncode, 1)
        self.assertIn(str(missing_plan), unreadable_show.stderr)

    def test_show_help_loads_outside_repository(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "show", "--help"],
            cwd=self.root,
            check=False,
            capture_output=True,
            text=True,
            env=self.environment(),
        )

        self.assertEqual(result.returncode, 0)
        self.assertIn("--session-dir", result.stdout)


if __name__ == "__main__":
    _ = unittest.main()
