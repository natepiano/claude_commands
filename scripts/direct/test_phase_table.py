#!/usr/bin/env python3
"""Tests for the phase table built from worker state and durable events."""

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

from scripts.direct import phase_table
from scripts.direct.recorder_fixture import invoke_recorder
from scripts.lib.socket_test_support import short_socket_directory
from scripts.production import fake_showrunner, fake_unit_host, showrunners


SCRIPT = Path(__file__).with_name("phase_table.py")


def plan_text(*lines: str) -> str:
    return "\n".join((*lines, ""))


def setUpModule() -> None:
    """The times these tests expect are UTC; the user's zone setting must not change them."""
    unittest.enterModuleContext(patch.dict(os.environ, {"USER_ZONE": "UTC"}))


class PhaseTableTests(unittest.TestCase):
    temporary: tempfile.TemporaryDirectory[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    history_dir: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    working_dir: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    session_dir: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    plan: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    vault_root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
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
                "UNIT_HISTORY_DIR": str(self.history_dir),
                "UNIT_NOW_EPOCH": str(at),
            },
        ):
            return phase_table.build(self.session_dir)

    def environment(self, at: int = 1_200) -> dict[str, str]:
        return {
            **os.environ,
            "NOTIFIER_STATE_DIR": str(self.notifier_dir),
            "NOTIFIER_SESSIONS_DIR": str(self.sessions_dir),
            "PHASE_TABLE_VAULT": str(self.vault_root),
            "UNIT_HISTORY_DIR": str(self.history_dir),
            "UNIT_NOW_EPOCH": str(at),
            **fake_unit_host.environment(self.root),
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

    def write_units_production(
        self,
        *rows: tuple[str, str, Path],
        checkout: Path | None = None,
    ) -> Path:
        path = self.working_dir / "docs" / "show-production.md"
        checkout_path = checkout or self.working_dir
        table_rows = [
            f"| {unit} | {plan} | {worktree} | branch | file.py |"
            for unit, plan, worktree in rows
        ]
        _ = path.write_text(
            plan_text(
                "# Production — show",
                "",
                "## Production Context",
                "",
                "- **Merge branch:** main",
                f"- **Showrunner checkout:** {checkout_path}",
                "- **Log:** docs/show.log",
                "- **User zone:** America/Los_Angeles",
                "",
                "## Units",
                "",
                "| Unit | Plan | Worktree | Branch | Owns |",
                "| --- | --- | --- | --- | --- |",
                *table_rows,
            ),
            encoding="utf-8",
        )
        return path

    def make_live_worktree(self, name: str) -> Path:
        worktree = self.root / name
        worktree.mkdir()
        _ = (worktree / ".git").write_text("gitdir: test\n", encoding="utf-8")
        return worktree

    def write_run_for_plan(
        self,
        name: str,
        plan: Path,
        worktree: Path,
        started_at: int,
        *events: dict[str, object],
    ) -> None:
        path = self.history_dir / "runs" / f"{name}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        started = {
            "event_type": "run_started",
            "working_dir": str(worktree),
            "plan_doc": str(plan.relative_to(worktree)),
            "run_started_at": started_at,
            "timestamp_epoch": started_at,
        }
        lines = [json.dumps(started), *(json.dumps(event) for event in events)]
        _ = path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def run_show_production(
        self, production: Path, at: int = 1_200
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "show",
                "--production-doc",
                str(production),
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=self.environment(at),
        )

    def run_prune(
        self, production: Path
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "prune",
                "--production-doc",
                str(production),
            ],
            check=False,
            capture_output=True,
            text=True,
            env=self.environment(),
        )

    def run_archive(
        self, production: Path
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "archive",
                "--production-doc",
                str(production),
            ],
            check=False,
            capture_output=True,
            text=True,
            env=self.environment(),
        )

    def production_file_line(self, production: str = "show") -> str:
        doc = (self.working_dir / "docs" / f"{production}-production.md").resolve()
        return f'production_file: "{doc.as_uri()}"'

    def write_phase_note(
        self,
        folder: str,
        name: str,
        production: str,
        unit: str,
    ) -> Path:
        path = self.vault_root / folder / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(
            "\n".join(
                (
                    "---",
                    "phase_table: true",
                    f"production: {production}",
                    f"unit: {unit}",
                    "---",
                    "",
                )
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
        fake_unit_host.write(self.environment(), [fake_unit_host.HostedUnit(
            "table-host", "w1:p1", {"SHOWRUNNER_UNIT": "show", "SHOWRUNNER_UNIT_ID": "table-unit"})])
        if not live_name:
            return
        pid = fake_unit_host.live_pid(self)
        fake_unit_host.place_process(self.environment(), pid, "w1:p1")
        self.live_record_number += 1
        socket_path = Path(self.enterContext(short_socket_directory())) / f"live-{self.live_record_number}.sock"
        held = socket.socket(socket.AF_UNIX)
        held.bind(str(socket_path))
        self.addCleanup(held.close)
        _ = (self.sessions_dir / f"record-{self.live_record_number}.json").write_text(
            json.dumps(
                {
                    "pid": pid,
                    "sessionId": f"live-{self.live_record_number}",
                    "name": live_name,
                    "messagingSocketPath": str(socket_path),
                    "updatedAt": self.live_record_number,
                    "tmux": "",
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

    def test_legacy_stated_eta_wins_over_a_later_progress_report(self) -> None:
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

    def test_checkpoint_eta_applies_to_its_named_phase_after_that_phase_starts(self) -> None:
        self.write_plan(
            plan_text(
                "## Delegation Context",
                "",
                "- **Project:** Checkpoint ETA fixture",
                "",
                "### Phase 2 — Current delivery  · status: todo",
                "",
                "### Phase 3 — Next delivery  · status: todo",
            )
        )

        def record(at: int, command: str, *arguments: str) -> None:
            environment = {
                **self.environment(at),
                "HOME": str(self.root),
                "USER_ZONE": "UTC",
                "UNIT_SYNC_WRITES": "0",
            }
            _ = environment.pop("CODEX_THREAD_ID", None)
            _ = environment.pop("CLAUDE_CODE_SESSION_ID", None)
            result = invoke_recorder(
                [command, "--session-dir", str(self.session_dir), *arguments],
                environment,
                self.working_dir,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        record(
            900, "start-run", "--working-dir", str(self.working_dir),
            "--plan-doc", str(self.plan), "--main-model", "fixture-model",
        )
        record(1_000, "start-phase", "--phase-id", "2", "--phase-title", "Current delivery")
        record(1_050, "eta", "--phase-id", "3", "--time", "00:30", "--basis", "checkpoint")

        previous = self.build(1_060).current

        assert isinstance(previous, phase_table.OpenPhase)
        self.assertEqual(previous.phase, "2")
        self.assertIsInstance(previous.eta, phase_table.EtaUnavailable)
        self.assertIsInstance(previous.first_stated, phase_table.EtaNeverStated)

        record(1_100, "finish-phase", "--status", "completed")
        record(1_150, "start-phase", "--phase-id", "3", "--phase-title", "Next delivery")

        current = self.build(1_200).current

        assert isinstance(current, phase_table.OpenPhase)
        self.assertEqual(current.phase, "3")
        assert isinstance(current.eta, phase_table.StatedEta)
        self.assert_epoch(current.eta.time, 1_800)
        self.assert_epoch(current.eta.stated_at, 1_050)
        self.assertEqual(current.eta.basis, "checkpoint")
        assert isinstance(current.first_stated, phase_table.FirstStatedEtaTarget)
        self.assert_epoch(current.first_stated.time, 1_800)

    def test_passed_stated_eta_ends_at_now_and_keeps_first(self) -> None:
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
        assert isinstance(current.eta, phase_table.StatedEta)
        self.assert_epoch(current.eta.time, 1_160)
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

    def eta_chain_fixture(self, *, gate: str = "", production: bool = True) -> None:
        """Ten-minute recorded work, a two-minute gap, then the Phase 5 promises."""
        lines = [
            "### Phase 4 — Finished delivery  · status: done",
            "",
            "### Phase 5 — Current delivery  · status: todo",
            "",
            "### Phase 6 — Next delivery  · status: todo",
            "",
        ]
        if gate:
            lines += ["**Blocked by:** G2", ""]
        lines += ["### Phase 7 — Last delivery  · status: todo"]
        if production:
            production_doc = self.write_production()
            _ = production_doc.write_text(
                production_doc.read_text(encoding="utf-8")
                + plan_text(
                    "", "## Gates", "",
                    "| Gate | Waiting | Waits on | Clears when |",
                    "| --- | --- | --- | --- |",
                    f"| G2 | this unit phase 6 | renderer phase 9 | {gate} |",
                ),
                encoding="utf-8",
            )
            lines.insert(
                0,
                "> **Production: show** — unit `unit-a`; production doc `docs/show-production.md`",
            )
        self.write_plan(plan_text(*lines))
        _ = self.write_run(
            "current", self.chain_time(16, 0),
            self.phase_event("phase_started", "4", "four", self.chain_time(16, 18)),
            self.phase_event(
                "phase_finished", "4", "four", self.chain_time(16, 28),
                status="completed", phase_elapsed_seconds=600,
            ),
            self.phase_event("phase_started", "5", "five", self.chain_time(16, 30)),
            *[
                self.phase_event(
                    "eta_stated", "5", "five", self.chain_time(*stated),
                    eta_phase_id="5", eta_at=self.chain_time(*target), basis="stated",
                )
                for stated, target in (
                    ((17, 0), (17, 15)),
                    ((17, 10), (17, 45)),
                    ((17, 40), (18, 30)),
                )
            ],
        )
        self.write_state(None)

    @staticmethod
    def chain_time(hour: int, minute: int) -> int:
        return int(datetime(2026, 10, 16, hour, minute, tzinfo=ZoneInfo("UTC")).timestamp())

    def test_latest_stated_eta_chains_two_todo_phases_with_recorded_gaps(self) -> None:
        self.eta_chain_fixture()

        record = self.build(self.chain_time(18, 0))
        for row, start, finish in zip(
            record.phases[2:], ((18, 32), (18, 44)), ((18, 42), (18, 54)), strict=True,
        ):
            assert isinstance(row, phase_table.TodoPhase)
            assert isinstance(row.times, phase_table.PredictedPhaseTiming)
            self.assert_epoch(row.times.start, self.chain_time(*start))
            self.assert_epoch(row.times.finish, self.chain_time(*finish))
        assert isinstance(record.plan_finish, phase_table.PredictedFinish)
        self.assert_epoch(record.plan_finish.at, self.chain_time(18, 54))

    def test_passed_stated_eta_chains_from_now_instead_of_a_past_finish(self) -> None:
        self.eta_chain_fixture()

        record = self.build(self.chain_time(18, 35))
        next_phase = record.phases[2]
        assert isinstance(next_phase, phase_table.TodoPhase)
        assert isinstance(next_phase.times, phase_table.PredictedPhaseTiming)
        self.assert_epoch(next_phase.times.start, self.chain_time(18, 37))
        self.assert_epoch(next_phase.times.finish, self.chain_time(18, 47))
        assert isinstance(record.plan_finish, phase_table.PredictedFinish)
        self.assert_epoch(record.plan_finish.at, self.chain_time(18, 59))

    def test_timed_gate_delays_the_chain_and_names_the_release_time(self) -> None:
        self.eta_chain_fixture(gate="release 2026-10-16 19:00; fallback 2026-10-16 20:00")

        record = self.build(self.chain_time(18, 0))
        gated = record.phases[2]
        assert isinstance(gated, phase_table.TodoPhase)
        assert isinstance(gated.times, phase_table.PredictedPhaseTiming)
        self.assert_epoch(gated.times.start, self.chain_time(19, 0))
        self.assert_epoch(gated.times.finish, self.chain_time(19, 10))
        assert isinstance(record.plan_finish, phase_table.PredictedFinish)
        self.assert_epoch(record.plan_finish.at, self.chain_time(19, 22))
        rendered = phase_table.render(record, ZoneInfo("UTC"))
        gated_row = next(line for line in rendered.splitlines() if line.startswith("| 6 |"))
        self.assertIn("gated until Fri 16 Oct 19:00", gated_row)

    def test_untimed_gate_removes_times_from_itself_and_every_later_phase(self) -> None:
        self.eta_chain_fixture(gate="renderer checkpoint and review")

        record = self.build(self.chain_time(18, 0))
        rendered = phase_table.render(record, ZoneInfo("UTC"))

        for phase in ("6", "7"):
            row = next(line for line in rendered.splitlines() if line.startswith(f"| {phase} |"))
            cells = [cell.strip() for cell in row.strip("|").split("|")]
            self.assertEqual(cells[2:], ["gated by G2: renderer phase 9", "—", "—"])

    def test_timed_gate_uses_the_productions_local_zone(self) -> None:
        self.eta_chain_fixture(gate="release 2026-10-16 19:00")
        production = self.working_dir / "docs" / "show-production.md"
        _ = production.write_text(
            production.read_text(encoding="utf-8").replace(
                "**User zone:** UTC", "**User zone:** America/New_York",
            ),
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"USER_ZONE": "America/New_York"}):
            record = self.build(self.chain_time(18, 0))

        gated = record.phases[2]
        assert isinstance(gated, phase_table.TodoPhase)
        assert isinstance(gated.times, phase_table.PredictedPhaseTiming)
        self.assert_epoch(gated.times.start, self.chain_time(23, 0))
        rendered = phase_table.render(record, ZoneInfo("America/New_York"))
        row = next(line for line in rendered.splitlines() if line.startswith("| 6 |"))
        self.assertIn("gated until Fri 16 Oct 19:00", row)

    def test_gate_release_before_the_chain_does_not_pull_a_phase_earlier(self) -> None:
        self.eta_chain_fixture(gate="expected 2026-10-16 17:00")

        record = self.build(self.chain_time(18, 0))
        next_phase = record.phases[2]
        assert isinstance(next_phase, phase_table.TodoPhase)
        assert isinstance(next_phase.times, phase_table.PredictedPhaseTiming)
        self.assert_epoch(next_phase.times.start, self.chain_time(18, 32))

    def test_cleared_gate_does_not_delay_predictions(self) -> None:
        self.eta_chain_fixture(gate="cleared 2026-10-16 19:00 — merged")

        record = self.build(self.chain_time(18, 0))
        next_phase = record.phases[2]
        assert isinstance(next_phase, phase_table.TodoPhase)
        assert isinstance(next_phase.times, phase_table.PredictedPhaseTiming)
        self.assert_epoch(next_phase.times.start, self.chain_time(18, 32))
        self.assertNotIn("gated", phase_table.render(record, ZoneInfo("UTC")))

    def test_blocked_by_is_ignored_without_a_production_document(self) -> None:
        self.eta_chain_fixture(gate="renderer checkpoint", production=False)

        record = self.build(self.chain_time(18, 0))
        next_phase = record.phases[2]
        assert isinstance(next_phase, phase_table.TodoPhase)
        assert isinstance(next_phase.times, phase_table.PredictedPhaseTiming)
        self.assert_epoch(next_phase.times.start, self.chain_time(18, 32))

    def test_phase_history_counts_moves_fixes_and_findings_across_attempts(self) -> None:
        self.write_plan(plan_text(
            "### Phase 1 — Repeated delivery  · status: done", "",
            "### Phase 2 — Current delivery  · status: todo",
        ))
        _ = self.write_run(
            "older", 800,
            self.phase_event("eta_stated", "", "", 850, eta_phase_id="1", eta_at=1_200),
            self.phase_event("phase_started", "1", "old-one", 900),
            self.phase_event("pass_started", "1", "old-one", 920, pass_kind="fix"),
            self.phase_event("finding_opened", "1", "old-one", 930, severity="blocker"),
            self.phase_event("finding_opened", "1", "old-one", 940, severity="minor"),
            self.phase_event(
                "phase_finished", "1", "old-one", 1_100,
                status="completed", phase_elapsed_seconds=200,
            ),
        )
        _ = self.write_run(
            "current", 1_300,
            self.phase_event("eta_stated", "", "", 1_350, eta_phase_id="1", eta_at=1_700),
            self.phase_event("phase_started", "1", "new-one", 1_400),
            self.phase_event("pass_started", "1", "new-one", 1_420, pass_kind="review"),
            self.phase_event("pass_started", "1", "new-one", 1_430, pass_kind="fix"),
            self.phase_event("finding_opened", "1", "new-one", 1_440, severity="minor"),
            self.phase_event("eta_stated", "1", "new-one", 1_450, eta_phase_id="1", eta_at=1_700),
            self.phase_event("eta_stated", "1", "new-one", 1_500, eta_phase_id="1", eta_at=1_800),
            self.phase_event("finding_opened", "1", "new-one", 1_505, severity="nit"),
            self.phase_event("eta_stated", "1", "new-one", 1_510, eta_phase_id="1", eta_at=1_800),
            self.phase_event("finding_verdict", "1", "new-one", 1_520, severity="blocker", verdict="fixed"),
            self.phase_event("eta_stated", "1", "new-one", 1_550, eta_phase_id="1", eta_at=1_650),
            self.phase_event("pass_started", "1", "new-one", 1_600, pass_kind="fix"),
            self.phase_event(
                "phase_finished", "1", "new-one", 1_770,
                status="completed", phase_elapsed_seconds=370,
            ),
            self.phase_event("eta_stated", "1", "new-one", 1_790, eta_phase_id="2", eta_at=2_100),
            self.phase_event("phase_started", "2", "two", 1_800),
            self.phase_event("pass_started", "2", "two", 1_810, pass_kind="fix"),
            self.phase_event("finding_opened", "2", "two", 1_820, severity="nit"),
        )
        self.write_state(None)

        record = self.build(1_900)
        done = record.phases[0]
        current = record.phases[1]
        assert isinstance(done, phase_table.DonePhase)
        assert isinstance(current, phase_table.OpenPhase)

        self.assert_epoch(done.first_eta, 1_200)
        self.assertEqual(done.moves, 3)
        self.assertEqual(
            [(int(target.timestamp()), int(stated.timestamp())) for target, stated in done.eta_trail],
            [(1_200, 850), (1_700, 1_350), (1_700, 1_450), (1_800, 1_500),
             (1_800, 1_510), (1_650, 1_550)],
        )
        self.assert_epoch(done.finished_at, 1_770)
        self.assertEqual(done.delta_minutes, 2.0)
        self.assertEqual(done.fix_rounds, 3)
        self.assertEqual(done.findings, {"blocker": 1, "minor": 2, "nit": 1})
        self.assert_epoch(current.first_eta, 2_100)
        self.assertEqual(current.moves, 0)
        self.assertEqual(
            [(int(target.timestamp()), int(stated.timestamp())) for target, stated in current.eta_trail],
            [(2_100, 1_790)],
        )
        self.assertIsInstance(current.finished_at, phase_table.PhaseNotFinished)
        self.assertIsInstance(current.delta_minutes, phase_table.FinishDeltaUnavailable)
        self.assertEqual(current.fix_rounds, 1)
        self.assertEqual(current.findings, {"blocker": 0, "minor": 0, "nit": 1})

    def test_fix_rounds_split_completed_and_running_passes_across_instances(self) -> None:
        self.write_plan(plan_text("### Phase 1 — Repeated delivery  · status: todo"))
        _ = self.write_run(
            "older", 800,
            self.phase_event("phase_started", "1", "old-one", 900,
                             phase_title="Repeated delivery"),
            self.phase_event("pass_started", "1", "old-one", 920,
                             pass_instance_id="old-fix", pass_kind="fix"),
            self.phase_event("pass_finished", "1", "old-one", 950,
                             pass_instance_id="old-fix", pass_kind="fix",
                             status="completed", pass_elapsed_seconds=30),
            self.phase_event("phase_finished", "1", "old-one", 1_000,
                             status="completed", phase_elapsed_seconds=100),
        )
        events = [
            self.phase_event("phase_started", "1", "new-one", 1_200,
                             phase_title="Repeated delivery"),
            self.phase_event("pass_started", "1", "new-one", 1_220,
                             pass_instance_id="review", pass_kind="review"),
            self.phase_event("pass_finished", "1", "new-one", 1_250,
                             pass_instance_id="review", pass_kind="review",
                             status="completed", pass_elapsed_seconds=30),
            self.phase_event("pass_started", "1", "new-one", 1_260,
                             pass_instance_id="finished-fix", pass_kind="fix"),
            self.phase_event("pass_finished", "1", "new-one", 1_300,
                             pass_instance_id="finished-fix", pass_kind="fix",
                             status="completed", pass_elapsed_seconds=40),
            self.phase_event("pass_started", "1", "new-one", 1_310,
                             pass_instance_id="running-fix", pass_kind="fix"),
        ]
        _ = self.write_run("current", 1_100, *events)
        self.write_state(None)

        current = self.build(1_400).current

        assert isinstance(current, phase_table.OpenPhase)
        self.assertEqual((current.fix_done, current.fix_running), (2, 1))
        self.assertEqual(current.fix_rounds, 3)

        events.append(self.phase_event(
            "pass_finished", "1", "new-one", 1_450,
            pass_instance_id="running-fix", pass_kind="fix",
            status="completed", pass_elapsed_seconds=140,
        ))
        _ = self.write_run("current", 1_100, *events)
        current = self.build(1_500).current

        assert isinstance(current, phase_table.OpenPhase)
        self.assertEqual((current.fix_done, current.fix_running), (3, 0))
        self.assertEqual(current.fix_rounds, 3)

    def test_running_fix_indicator_is_binary_while_total_keeps_all_open_passes(self) -> None:
        self.write_plan(plan_text("### Phase 1 — Current delivery  · status: todo"))
        events = [
            self.phase_event("phase_started", "1", "one", 900,
                             phase_title="Current delivery"),
            self.phase_event("pass_started", "1", "one", 920,
                             pass_instance_id="finished", pass_kind="fix"),
            self.phase_event("pass_finished", "1", "one", 950,
                             pass_instance_id="finished", status="completed"),
            self.phase_event("pass_started", "1", "one", 960,
                             pass_instance_id="first-open", pass_kind="fix"),
            self.phase_event("pass_started", "1", "one", 980,
                             pass_instance_id="second-open", pass_kind="fix"),
        ]
        self.write_state(None)

        for closed, expected_done in ((0, 1), (1, 2), (2, 3)):
            with self.subTest(closed=closed):
                _ = self.write_run("current", 800, *events)
                current = self.build(1_200).current

                assert isinstance(current, phase_table.OpenPhase)
                self.assertEqual(current.fix_done, expected_done)
                self.assertEqual(current.fix_running, 0 if closed == 2 else 1)
                self.assertEqual(current.fix_rounds, 3)

                if closed < 2:
                    events.append(self.phase_event(
                        "pass_finished", "1", "one", 1_000 + closed * 100,
                        pass_instance_id="first-open" if closed == 0 else "second-open",
                        status="completed",
                    ))

    def test_phase_finished_before_its_last_eta_has_a_negative_delta(self) -> None:
        self.write_plan(plan_text("### Phase 1 — Finished delivery  · status: done"))
        _ = self.write_run(
            "current", 800,
            self.phase_event("phase_started", "1", "one", 900),
            self.phase_event("eta_stated", "1", "one", 1_000, eta_phase_id="1", eta_at=1_800),
            self.phase_event(
                "phase_finished", "1", "one", 1_500,
                status="completed", phase_elapsed_seconds=600,
            ),
        )
        self.write_state(None)

        done = self.build(1_600).phases[0]

        assert isinstance(done, phase_table.DonePhase)
        self.assertEqual(done.delta_minutes, -5.0)

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

    def test_reused_phase_number_history_matches_the_rows_title_family(self) -> None:
        for state in ("done", "open", "todo"):
            for earlier_attempt in (False, True):
                with self.subTest(state=state, earlier_attempt=earlier_attempt):
                    self.write_plan(plan_text(
                        "### Phase 9 — Replacement delivery  · status: "
                        + ("done" if state == "done" else "todo")
                    ))
                    events = [
                        self.phase_event("phase_started", "9", "retired", 1_000,
                                         phase_title="Retired investigation"),
                        self.phase_event("eta_stated", "9", "retired", 1_050,
                                         eta_phase_id="9", eta_at=1_500),
                        self.phase_event("pass_started", "9", "retired", 1_060,
                                         pass_kind="fix"),
                        self.phase_event("finding_opened", "9", "retired", 1_070,
                                         severity="blocker"),
                        self.phase_event("phase_finished", "9", "retired", 1_200,
                                         status="completed", phase_elapsed_seconds=200),
                    ]
                    if earlier_attempt:
                        events += [
                            self.phase_event("phase_started", "9", "earlier", 2_000,
                                             phase_title="`REPLACEMENT   delivery`"),
                            self.phase_event("eta_stated", "9", "earlier", 2_050,
                                             eta_phase_id="9", eta_at=3_000),
                            self.phase_event("pass_started", "9", "earlier", 2_060,
                                             pass_kind="fix"),
                            self.phase_event("finding_opened", "9", "earlier", 2_070,
                                             severity="minor"),
                            self.phase_event("phase_finished", "9", "earlier", 2_200,
                                             status="stopped", phase_elapsed_seconds=200),
                        ]
                    events.append(self.phase_event(
                        "phase_started", "9", "replacement", 2_400,
                        phase_title="Replacement delivery",
                    ))
                    if state != "open":
                        events.append(self.phase_event(
                            "phase_finished", "9", "replacement", 2_600,
                            status="completed" if state == "done" else "stopped",
                            phase_elapsed_seconds=200,
                        ))
                    _ = self.write_run("history", 900, *events)
                    self.write_state(None)

                    row = self.build(2_700).phases[0]

                    if earlier_attempt:
                        self.assert_epoch(row.first_eta, 3_000)
                        self.assertEqual(
                            [(int(target.timestamp()), int(stated.timestamp()))
                             for target, stated in row.eta_trail],
                            [(3_000, 2_050)],
                        )
                    else:
                        self.assertIsInstance(row.first_eta, phase_table.EtaNeverStated)
                        self.assertEqual(row.eta_trail, [])
                    self.assertEqual(row.fix_rounds, int(earlier_attempt))
                    self.assertEqual(row.findings, {
                        "blocker": 0, "minor": int(earlier_attempt), "nit": 0,
                    })
                    if state == "done" and earlier_attempt:
                        assert isinstance(row.delta_minutes, float)
                        self.assertAlmostEqual(row.delta_minutes, -400 / 60)
                    else:
                        self.assertIsInstance(row.delta_minutes, phase_table.FinishDeltaUnavailable)
                    if isinstance(row, phase_table.DonePhase):
                        assert isinstance(row.times, phase_table.CompletedPhaseTiming)
                        self.assert_epoch(row.times.start, 2_000 if earlier_attempt else 2_400)
                        self.assertEqual(row.times.seconds, 400 if earlier_attempt else 200)
                    elif isinstance(row, phase_table.OpenPhase):
                        self.assert_epoch(row.started, 2_000 if earlier_attempt else 2_400)

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

    def test_run_phase_records_match_plan_rows_with_repairs_findings_and_moved_etas(self) -> None:
        headings = plan_text(
            "### Phase 1 — Repair delivery  · status: done", "",
            "### Phase 2 — Moved delivery  · status: done", "",
            "### Phase 3 — Current delivery  · status: todo",
        )
        for target in (2_200, 1_900, None):
            with self.subTest(open_eta=target):
                self.write_plan(headings)
                events = [
                    self.phase_event("eta_stated", "", "", 850, eta_phase_id="1", eta_at=1_200),
                    self.phase_event("phase_started", "1", "one", 900, phase_title="Repair delivery"),
                    self.phase_event("pass_started", "1", "one", 950,
                                     pass_kind="fix", pass_instance_id="repair", fix_pass=1),
                    self.phase_event("finding_opened", "1", "one", 1_000, severity="minor"),
                    self.phase_event("pass_finished", "1", "one", 1_100, pass_instance_id="repair"),
                    self.phase_event("phase_finished", "1", "one", 1_200,
                                     status="completed", phase_elapsed_seconds=300),
                    self.phase_event("phase_started", "2", "two", 1_300, phase_title="Moved delivery"),
                    self.phase_event("eta_stated", "2", "two", 1_350, eta_phase_id="2", eta_at=1_500),
                    self.phase_event("eta_stated", "2", "two", 1_400, eta_phase_id="2", eta_at=1_600),
                    self.phase_event("phase_finished", "2", "two", 1_700,
                                     status="completed", phase_elapsed_seconds=400),
                ]
                if target is not None:
                    events.append(self.phase_event("eta_stated", "2", "two", 1_750,
                                                   eta_phase_id="3", eta_at=target))
                events.append(self.phase_event("phase_started", "3", "three", 1_800,
                                               phase_title="Current delivery"))
                _ = self.write_run("current", 800, *events)
                with patch.dict(os.environ, {
                    "UNIT_HISTORY_DIR": str(self.history_dir),
                    "UNIT_NOW_EPOCH": "2000",
                }):
                    planned = phase_table.build_plan(self.plan)
                    self.write_plan("# As built\n")
                    recorded = phase_table.build_run(self.plan, events)

                self.assertEqual(recorded.phases, planned.phases)
                self.assertEqual(recorded.current, planned.current)
                self.assertEqual(recorded.plan_finish, planned.plan_finish)
                self.assertEqual(len(recorded.phases), 3)
                first, second, current = recorded.phases
                self.assertIsInstance(first, phase_table.DonePhase)
                self.assertEqual(first.fix_done, 1)
                self.assertEqual(first.findings, {"blocker": 0, "minor": 1, "nit": 0})
                self.assertEqual(second.moves, 1)
                self.assertEqual(second.delta_minutes, 100 / 60)
                self.assertIsInstance(current, phase_table.OpenPhase)

    def test_open_phase_survives_a_title_supplied_by_a_later_progress_event(self) -> None:
        self.write_plan(plan_text("### Phase 1 — Delivery  · status: todo"))
        events = [
            self.phase_event("phase_started", "1", "one", 100),
            self.phase_event("progress_reported", "1", "one", 150,
                             phase_percent=50, phase_elapsed_seconds=50),
        ]
        with patch.dict(os.environ, {
            "UNIT_HISTORY_DIR": str(self.history_dir),
            "UNIT_NOW_EPOCH": "150",
        }):
            _ = self.write_run("current", 50, *events)
            without_title = phase_table.build_plan(self.plan)
            events[1]["phase_title"] = "Delivery"
            _ = self.write_run("current", 50, *events)
            with_title = phase_table.build_plan(self.plan)

        self.assertEqual(with_title, without_title)
        self.assertIsInstance(with_title.current, phase_table.OpenPhase)
        self.assertEqual(with_title.phases, [with_title.current])
        assert isinstance(with_title.current, phase_table.OpenPhase)
        self.assert_epoch(with_title.current.started, 100)
        self.assertEqual(with_title.current.progress, phase_table.ReportedPhaseProgress(50))
        assert isinstance(with_title.current.eta, phase_table.ProjectedEta)
        self.assert_epoch(with_title.current.eta.time, 200)
        self.assert_epoch(with_title.current.schedule_end, 200)
        self.assertEqual(with_title.plan_finish, phase_table.PredictedFinish(
            datetime.fromtimestamp(200, ZoneInfo("UTC")),
        ))

    def test_run_records_keep_completions_without_start_or_elapsed_time(self) -> None:
        for has_start in (False, True):
            for elapsed in (None, 300):
                with self.subTest(has_start=has_start, elapsed=elapsed):
                    events: list[dict[str, object]] = []
                    if has_start:
                        events.append(self.phase_event("phase_started", "7", "one", 100,
                                                       phase_title="Delivery"))
                    events.append(self.phase_event("eta_stated", "7", "one", 150,
                                                   eta_phase_id="7", eta_at=800))
                    finished = self.phase_event("phase_finished", "7", "one", 1_000,
                                                phase_title="Delivery", status="completed")
                    if elapsed is not None:
                        finished["phase_elapsed_seconds"] = elapsed
                    events.extend([finished, {"event_type": "run_finished", "timestamp_epoch": 1_100}])

                    record = phase_table.build_run(self.plan, events)

                    self.assertEqual(len(record.phases), 1)
                    row = record.phases[0]
                    assert isinstance(row, phase_table.DonePhase)
                    self.assertEqual((row.phase, row.title), ("7", "Delivery"))
                    assert isinstance(row.times, phase_table.CompletedPhaseTiming)
                    self.assert_epoch(row.times.start, 100 if has_start else 1_000 - (elapsed or 0))
                    self.assert_epoch(row.times.finish, 1_000)
                    self.assertEqual(row.times.seconds, elapsed or 0)
                    self.assertEqual(row.eta_trail, [(
                        datetime.fromtimestamp(800, ZoneInfo("UTC")),
                        datetime.fromtimestamp(150, ZoneInfo("UTC")),
                    )])
                    self.assertEqual(row.delta_minutes, 200 / 60)
                    self.assertIsInstance(record.current, phase_table.NoOpenPhase)
                    self.assertEqual(record.plan_finish, phase_table.FinishedAt(row.times.finish))

    def test_run_records_use_completion_titles_instead_of_start_titles(self) -> None:
        for completion_title in ("Final delivery", ""):
            with self.subTest(completion_title=completion_title):
                events = [
                    self.phase_event("phase_started", "7", "one", 100,
                                     phase_title="Original delivery"),
                    self.phase_event("finding_opened", "7", "one", 150, severity="minor"),
                    self.phase_event("phase_finished", "7", "one", 200,
                                     phase_title=completion_title, phase_elapsed_seconds=100,
                                     status="completed"),
                ]

                record = phase_table.build_run(self.plan, events)

                self.assertEqual(len(record.phases), 1)
                row = record.phases[0]
                self.assertIsInstance(row, phase_table.DonePhase)
                self.assertEqual(row.title, completion_title or "Original delivery")
                self.assertEqual(row.findings, {"blocker": 0, "minor": 1, "nit": 0})

    def test_build_plan_needs_no_session_state(self) -> None:
        self.write_plan(plan_text("### Phase 1 — Delivery  · status: todo"))

        with patch.dict(
            os.environ,
            {
                "UNIT_HISTORY_DIR": str(self.history_dir),
                "UNIT_NOW_EPOCH": "1200",
            },
        ):
            record = phase_table.build_plan(self.plan)

        self.assertEqual(record.plan, self.plan.resolve())
        self.assertIsInstance(record.current, phase_table.NoOpenPhase)

    def parallel_plan(self, *phases: str) -> None:
        self.write_plan(plan_text("### Phase 1 — Base  · status: done", *phases))
        _ = self.write_run(
            "current",
            900,
            self.phase_event("phase_started", "2", "two", 1_000, phase_title="Parser"),
            self.phase_event("phase_started", "3", "three", 1_050, phase_title="Notice"),
        )
        self.write_state(None)

    def test_parallel_names_waiting_phases_whose_dependencies_are_done(self) -> None:
        self.parallel_plan(
            "### Phase 2 — Parser  · status: todo",
            "**Depends on:** Phase 1",
            "### Phase 3 — Notice  · status: todo",
            "**Depends on:** none",
            "### Phase 4 — Report  · status: todo",
            "**Depends on:** Phase 2",
            "### Phase 5 — Docs  · status: todo",
            "**Depends on:** Phase 1",
            "### Phase 6 — Gated  · status: todo",
            "**Blocked by:** G2 — trunk phase 3 merged",
            "**Depends on:** none",
        )

        check = self.build(1_100).parallel

        self.assertEqual(check, phase_table.CouldStart(("5",)))
        self.assertEqual(phase_table.parallel_line(check), "Parallel: Phase 5 could start")

    def test_parallel_ignores_the_date_and_time_after_a_phase_reference(self) -> None:
        self.parallel_plan(
            "### Phase 2 — Parser  · status: todo",
            "**Depends on:** Phase 1",
            "### Phase 3 — Notice  · status: todo",
            "**Depends on:** Phase 1",
            "### Phase 4 — Resources  · status: todo",
            "**Depends on:** Phase 1 (resources-unit waits until 2026-10-29 00:00)",
        )

        result = subprocess.run(
            [sys.executable, str(SCRIPT), "parallel", "--session-dir", str(self.session_dir)],
            env=self.environment(1_100),
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        self.assertEqual(result.stdout, "Parallel: Phase 4 could start\n")
        self.assertEqual(result.stderr, "")

    def test_parallel_waits_for_a_running_dependency_followed_by_a_period(self) -> None:
        self.write_plan(plan_text(
            "### Phase 7 — Parser  · status: todo",
            "**Depends on:** none",
            "### Phase 8 — Report  · status: todo",
            "**Depends on:** Phase 7.",
        ))
        _ = self.write_run(
            "current",
            900,
            self.phase_event("phase_started", "7", "seven", 1_000, phase_title="Parser"),
        )
        self.write_state(None)

        result = subprocess.run(
            [sys.executable, str(SCRIPT), "parallel", "--session-dir", str(self.session_dir)],
            env=self.environment(1_100),
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        self.assertEqual(result.stdout, "Parallel: none — Phase 8 needs Phase 7 (running)\n")
        self.assertEqual(result.stderr, "")

    def test_parallel_none_names_what_the_first_waiting_phase_needs(self) -> None:
        self.parallel_plan(
            "### Phase 2 — Parser  · status: todo",
            "**Depends on:** Phase 1",
            "### Phase 3 — Notice  · status: todo",
            "**Depends on:** Phase 2",
            "### Phase 4 — Report  · status: todo",
            "**Depends on:** Phase 2, Phase 5",
            "### Phase 5 — Docs  · status: todo",
            "**Blocked by:** G1 — trunk phase 2 merged",
            "**Depends on:** none",
        )

        line = phase_table.parallel_line(self.build(1_100).parallel)

        self.assertEqual(line, "Parallel: none — Phase 4 needs Phase 2 (running), Phase 5 (todo)")

    def test_parallel_is_unknown_while_a_waiting_phase_states_no_dependencies(self) -> None:
        self.parallel_plan(
            "### Phase 2 — Parser  · status: todo",
            "### Phase 3 — Notice  · status: todo",
            "### Phase 4 — Report  · status: todo",
            "**Depends on:** Phase 1",
            "### Phase 5 — Docs  · status: todo",
        )

        line = phase_table.parallel_line(self.build(1_100).parallel)

        self.assertEqual(
            line,
            "Parallel: unknown — Phase 5 has no **Depends on:** line; "
            + "add one under each phase heading and rerun",
        )

    def test_parallel_with_nothing_waiting(self) -> None:
        self.parallel_plan("### Phase 2 — Parser  · status: todo")

        line = phase_table.parallel_line(self.build(1_100).parallel)

        self.assertEqual(line, "Parallel: none — no phase is waiting")

    def test_production_json_carries_the_phases_that_could_start(self) -> None:
        self.parallel_plan(
            "### Phase 2 — Parser  · status: todo",
            "### Phase 4 — Report  · status: todo",
            "**Depends on:** Phase 1",
        )

        with patch.dict(
            os.environ,
            {
                "UNIT_HISTORY_DIR": str(self.history_dir),
                "UNIT_NOW_EPOCH": "1100",
            },
        ):
            shown = cast(dict[str, object], json.loads(phase_table.show(self.session_dir, ZoneInfo("UTC"), True)))

        self.assertEqual(
            shown["parallel"],
            {"could_start": ["4"], "line": "Parallel: Phase 4 could start"},
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
                "UNIT_HISTORY_DIR": str(self.history_dir),
                "UNIT_NOW_EPOCH": str(epoch(10, 0)),
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
                "UNIT_HISTORY_DIR": str(self.history_dir),
                "UNIT_NOW_EPOCH": str(epoch(10, 0)),
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
            },
        )
        self.assertEqual(eta_json["source"], "stated")
        self.assertIsNone(eta_json["earliest"])
        self.assertIsNone(eta_json["latest"])
        self.assertIsNone(eta_json["as_of"])
        self.assertEqual(eta_json["basis"], "two checks remain")
        self.assertNotIn("first", eta_json)
        self.assertEqual(
            eta_json["time"], current_json["first_stated_eta_target"]
        )
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
                "UNIT_HISTORY_DIR": str(self.history_dir),
                "UNIT_NOW_EPOCH": str(epoch(10, 0)),
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
                "UNIT_HISTORY_DIR": str(self.history_dir),
                "UNIT_NOW_EPOCH": str(epoch(10, 0)),
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
                "UNIT_HISTORY_DIR": str(self.history_dir),
                "UNIT_NOW_EPOCH": "1300",
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
        environment["UNIT_HISTORY_DIR"] = str(self.history_dir)
        environment["UNIT_NOW_EPOCH"] = str(reported_at)

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
            {"plan", "updated", "plan_finish", "current", "phases", "parallel"},
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
            },
        )
        self.assertEqual(eta["source"], "projected")
        self.assertIsNone(eta["stated_at"])
        self.assertIsNone(eta["basis"])
        self.assertTrue(cast(str, eta["as_of"]).endswith("-07:00"))
        self.assertNotIn("first", eta)
        self.assertIsNone(current["first_stated_eta_target"])

    def test_json_counts_repair_rounds_from_the_first_eta_statement(self) -> None:
        self.write_plan(plan_text("### Phase 1 — Current delivery  · status: todo"))
        _ = self.write_run(
            "current",
            900,
            self.phase_event("phase_started", "1", "one", 1_000),
            self.phase_event(
                "pass_started", "1", "one", 1_010, pass_kind="fix", fix_pass=1
            ),
            self.phase_event(
                "eta_stated",
                "1",
                "one",
                1_050,
                eta_at=1_500,
                basis="first promise",
            ),
            self.phase_event(
                "pass_started", "1", "one", 1_060, pass_kind="review", fix_pass=1
            ),
            self.phase_event(
                "pass_started", "1", "one", 1_070, pass_kind="fix", fix_pass=2
            ),
            self.phase_event(
                "pass_started", "1", "one", 1_080, pass_kind="review", fix_pass=2
            ),
            self.phase_event(
                "pass_started", "1", "one", 1_090, pass_kind="fix", fix_pass=3
            ),
            self.phase_event(
                "pass_started", "1", "one", 1_100, pass_kind="test", fix_pass=0
            ),
            self.phase_event(
                "pass_started", "1", "other", 1_110, pass_kind="fix", fix_pass=4
            ),
            self.phase_event(
                "eta_stated",
                "1",
                "one",
                1_120,
                eta_at=1_700,
                basis="restated after repair",
            ),
        )
        self.write_state(None)

        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "show",
                "--session-dir",
                str(self.session_dir),
                "--zone",
                "UTC",
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=self.environment(1_200),
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        parsed = cast("dict[str, object]", json.loads(result.stdout))
        current = cast("dict[str, object]", parsed["current"])
        eta = cast("dict[str, object]", current["eta"])
        self.assertEqual(
            current["first_stated_eta_target"], "1970-01-01T00:25:00+00:00"
        )
        self.assertEqual(current["repair_rounds_since_first_stated_eta"], 2)
        self.assertEqual(eta["time"], "1970-01-01T00:28:20+00:00")

    def test_json_does_not_count_a_round_started_with_the_first_eta(self) -> None:
        self.write_plan(plan_text("### Phase 1 — Current delivery  · status: todo"))
        stated = self.phase_event(
            "eta_stated", "1", "one", 1_000, eta_at=1_500, basis="first promise"
        )
        repair = self.phase_event(
            "pass_started", "1", "one", 1_000, pass_kind="fix", fix_pass=1
        )
        stated["timestamp_epoch"] = 1_000.0000004
        repair["timestamp_epoch"] = 1_000.0000004
        _ = self.write_run(
            "current",
            900,
            self.phase_event("phase_started", "1", "one", 990),
            stated,
            repair,
        )
        self.write_state(None)

        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "show",
                "--session-dir",
                str(self.session_dir),
                "--zone",
                "UTC",
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=self.environment(1_200),
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        parsed = cast("dict[str, object]", json.loads(result.stdout))
        current = cast("dict[str, object]", parsed["current"])
        self.assertEqual(current["repair_rounds_since_first_stated_eta"], 0)

    def test_json_keeps_first_stated_target_after_the_eta_passes(self) -> None:
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
                eta_at=1_100,
                basis="first promise",
            ),
            self.phase_event(
                "pass_started", "1", "one", 1_070, pass_kind="fix", fix_pass=1
            ),
        )
        self.write_state(None)

        with patch.dict(
            os.environ,
            {
                "UNIT_HISTORY_DIR": str(self.history_dir),
                "UNIT_NOW_EPOCH": "1200",
            },
        ):
            parsed = cast(
                "dict[str, object]",
                json.loads(
                    phase_table.show(
                        self.session_dir,
                        ZoneInfo("UTC"),
                        json_output=True,
                    )
                ),
            )

        current = cast("dict[str, object]", parsed["current"])
        eta = cast("dict[str, object]", current["eta"])
        self.assertEqual(eta["time"], "1970-01-01T00:20:00+00:00")
        self.assertEqual(
            current["first_stated_eta_target"], "1970-01-01T00:18:20+00:00"
        )
        self.assertEqual(current["repair_rounds_since_first_stated_eta"], 1)

    def test_json_marks_an_open_phase_whose_eta_was_never_stated(self) -> None:
        self.write_plan(plan_text("### Phase 1 — Current delivery  · status: todo"))
        _ = self.write_run(
            "current",
            900,
            self.phase_event("phase_started", "1", "one", 1_000),
        )
        self.write_state(None)

        with patch.dict(
            os.environ,
            {
                "UNIT_HISTORY_DIR": str(self.history_dir),
                "UNIT_NOW_EPOCH": "1200",
            },
        ):
            parsed = cast(
                "dict[str, object]",
                json.loads(
                    phase_table.show(
                        self.session_dir,
                        ZoneInfo("UTC"),
                        json_output=True,
                    )
                ),
            )

        current = cast("dict[str, object]", parsed["current"])
        self.assertIsNone(current["eta"])
        self.assertIsNone(current["first_stated_eta_target"])
        self.assertIsNone(current["repair_rounds_since_first_stated_eta"])

    def test_production_json_resolves_each_plan_from_its_unit_worktree(
        self,
    ) -> None:
        # The offset this test expects is Pacific.
        self.enterContext(patch.dict(os.environ, {"USER_ZONE": "America/Los_Angeles"}))
        worktree = self.make_live_worktree("unit-worktree")
        plan = worktree / "docs" / "unit-plan.md"
        plan.parent.mkdir()
        _ = plan.write_text(
            plan_text("### Phase 4 — Unit delivery  · status: todo"),
            encoding="utf-8",
        )
        self.write_run_for_plan(
            "unit",
            plan,
            worktree,
            900,
            self.phase_event(
                "phase_started",
                "4",
                "four",
                1_000,
                phase_title="Unit delivery",
            ),
        )
        showrunner_checkout = self.root / "showrunner-checkout"
        showrunner_checkout.mkdir()
        production = self.write_units_production(
            ("worker", "docs/unit-plan.md commentary", worktree),
            checkout=showrunner_checkout,
        )

        result = self.run_show_production(production)

        self.assertEqual(result.returncode, 0, result.stderr)
        records = cast("dict[str, object]", json.loads(result.stdout))
        record = cast("dict[str, object]", records["worker"])
        current = cast("dict[str, object]", record["current"])
        self.assertEqual(record["plan"], str(plan.resolve()))
        self.assertNotEqual(plan.parent, showrunner_checkout)
        self.assertEqual(current["phase"], "4")
        self.assertTrue(cast(str, current["started"]).endswith("-08:00"))

    def test_production_json_keeps_an_unreadable_plan_local_to_its_unit(
        self,
    ) -> None:
        readable_worktree = self.make_live_worktree("readable-worktree")
        readable_plan = readable_worktree / "plan.md"
        _ = readable_plan.write_text(
            plan_text("### Phase 1 — Ready  · status: todo"),
            encoding="utf-8",
        )
        missing_worktree = self.make_live_worktree("missing-worktree")
        production = self.write_units_production(
            ("ready", "plan.md", readable_worktree),
            ("missing", "missing.md", missing_worktree),
        )

        result = self.run_show_production(production)

        self.assertEqual(result.returncode, 0, result.stderr)
        records = cast("dict[str, object]", json.loads(result.stdout))
        ready = cast("dict[str, object]", records["ready"])
        missing = cast("dict[str, object]", records["missing"])
        self.assertIsNone(ready["current"])
        self.assertEqual(set(missing), {"unavailable"})
        self.assertIn(
            str(missing_worktree / "missing.md"),
            cast(str, missing["unavailable"]),
        )

    def test_production_json_omits_retired_rows(self) -> None:
        live_worktree = self.make_live_worktree("live-worktree")
        live_plan = live_worktree / "plan.md"
        _ = live_plan.write_text(
            plan_text("### Phase 1 — Live  · status: todo"),
            encoding="utf-8",
        )
        retired_worktree = self.root / "retired-worktree"
        production = self.write_units_production(
            ("live", "plan.md", live_worktree),
            ("retired", "retired plan.md", retired_worktree),
        )

        result = self.run_show_production(production)

        self.assertEqual(result.returncode, 0, result.stderr)
        records = cast("dict[str, object]", json.loads(result.stdout))
        self.assertEqual(set(records), {"live"})

    def test_cli_reports_missing_state_or_plan_on_one_line(self) -> None:
        empty_session = self.root / "empty-session"
        empty_session.mkdir()
        environment = os.environ.copy()
        environment["UNIT_HISTORY_DIR"] = str(self.history_dir)

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
                    "showrunner: showrunner",
                    self.production_file_line(),
                    "unit: table-unit",
                    'now: "Phase 1 of 1 — Current delivery"',
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

    def test_refresh_with_no_open_phase_writes_done_count_and_body_heading(self) -> None:
        self.write_production_plan()
        self.write_plan(
            plan_text(
                "# Delivery plan",
                "",
                "> **Production: show** — unit `table-unit`; production doc `docs/show-production.md`",
                "",
                "### Phase 1 — Prior delivery  · status: done",
                "",
                "### Phase 2 — Next delivery  · status: todo",
            )
        )
        _ = self.write_run(
            "current",
            900,
            self.phase_event("phase_started", "1", "one", 1_000, phase_title="Prior delivery"),
            self.phase_event(
                "phase_finished", "1", "one", 1_100,
                status="completed", phase_elapsed_seconds=100,
            ),
            {"event_type": "run_finished", "timestamp_epoch": 1_110},
        )
        self.mark_unit("table")

        result = self.run_refresh()

        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        lines = (self.vault_root / "showrunner" / "table.md").read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[5], 'now: "No phase running — 1 of 2 done"')
        heading = next(line for line in lines if line.startswith("**"))
        self.assertEqual(json.loads(lines[5].removeprefix("now: ")), heading[2:-2])

    def test_refresh_with_phase_outside_the_plan_drops_the_plan_count(self) -> None:
        self.write_production_plan()
        self.write_plan(
            plan_text(
                "# Delivery plan",
                "",
                "> **Production: show** — unit `table-unit`; production doc `docs/show-production.md`",
                "",
                "### Phase 1 — Prior delivery  · status: done",
                "",
                "### Phase 2 — Next delivery  · status: todo",
            )
        )
        _ = self.write_run(
            "current",
            900,
            self.phase_event("phase_started", "14", "other", 1_000, phase_title="Placement disk"),
        )
        self.mark_unit("table")

        result = self.run_refresh()

        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        lines = (self.vault_root / "showrunner" / "table.md").read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[5], 'now: "Phase 14 — Placement disk · not in this plan"')
        self.assertIn("**Phase 14 — Placement disk · not in this plan**", lines)

    def test_refresh_escapes_title_in_now_and_replaces_owned_note(self) -> None:
        self.write_production_plan()
        title = 'Delivery: "quoted" \\ path'
        self.write_plan(
            plan_text(
                "# Delivery plan",
                "",
                "> **Production: show** — unit `table-unit`; production doc `docs/show-production.md`",
                "",
                f"### Phase 1 — {title}  · status: todo",
            )
        )
        _ = self.write_run(
            "current", 900,
            self.phase_event("phase_started", "1", "one", 1_000, phase_title=title),
        )
        self.mark_unit("table")
        result = self.run_refresh()
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        note = self.vault_root / "showrunner" / "table.md"
        text = note.read_text(encoding="utf-8")
        self.assertEqual(
            text.splitlines()[5],
            'now: "Phase 1 of 1 — Delivery: \\"quoted\\" \\\\ path"',
        )
        self.assertIn(f"**Phase 1 of 1 — {title}**\n", text)
        _ = note.write_text(text + "old copy\n", encoding="utf-8")

        second = self.run_refresh(at=1_300)

        self.assertEqual((second.returncode, second.stdout, second.stderr), (0, "", ""))
        refreshed = note.read_text(encoding="utf-8")
        self.assertNotIn("old copy", refreshed)
        self.assertEqual(refreshed.splitlines()[4], text.splitlines()[4])
        self.assertIn(f"**Phase 1 of 1 — {title}**\n", refreshed)
        self.assertIn("| Updated | 01-01 00:21 UTC |", refreshed)

    def test_refresh_replaces_three_key_note_with_current_heading(self) -> None:
        self.write_production_plan()
        self.mark_unit("table")
        note = self.write_phase_note("showrunner", "table", "show", "table-unit")
        self.assertNotIn("now:", note.read_text(encoding="utf-8"))

        result = self.run_refresh()

        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertEqual(
            note.read_text(encoding="utf-8").splitlines()[:7],
            [
                "---",
                "phase_table: true",
                "showrunner: showrunner",
                self.production_file_line(),
                "unit: table-unit",
                'now: "Phase 1 of 1 — Current delivery"',
                "---",
            ],
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
        text = target.read_text(encoding="utf-8")
        self.assertIn("| Updated | 01-01 00:21 UTC |", text)
        self.assertIn("\nshowrunner: last-known-showrunner\n", text)
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

    def test_show_missing_production_document_is_one_line_refusal(self) -> None:
        production = self.working_dir / "docs" / "missing-production.md"

        result = self.run_show_production(production)

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertTrue(result.stderr.startswith("cannot read production doc: "))
        self.assertIn(str(production), result.stderr)
        self.assertNotIn("Traceback", result.stderr)

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

    def test_archive_moves_owned_notes_with_bytes_and_modes_unchanged(
        self,
    ) -> None:
        production = self.write_units_production()
        first = self.write_phase_note("runner-one", "alpha", "show", "alpha-unit")
        second = self.write_phase_note("runner-two", "beta", "show", "beta-unit")
        first_bytes = (
            b"---\nphase_table: true\nproduction: show\nunit: alpha-unit\n---\n"
            b"# Alpha\n\nFirst table.\n"
        )
        second_bytes = (
            b"---\nphase_table: true\nproduction: show\nunit: beta-unit\n---\n"
            b"# Beta\n\nSecond table.\n"
        )
        _ = first.write_bytes(first_bytes)
        _ = second.write_bytes(second_bytes)
        first.chmod(0o640)
        second.chmod(0o600)
        first_target = (
            self.vault_root.parent / "archive" / "runner-one" / "show" / "alpha.md"
        )
        second_target = (
            self.vault_root.parent / "archive" / "runner-two" / "show" / "beta.md"
        )

        result = self.run_archive(production)

        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertCountEqual(
            result.stdout.splitlines(),
            (str(first_target), str(second_target)),
        )
        self.assertEqual(first_target.read_bytes(), first_bytes)
        self.assertEqual(second_target.read_bytes(), second_bytes)
        self.assertEqual(first_target.stat().st_mode & 0o777, 0o640)
        self.assertEqual(second_target.stat().st_mode & 0o777, 0o600)
        self.assertFalse(first.parent.exists())
        self.assertFalse(second.parent.exists())

    def test_archive_leaves_notes_it_does_not_own_untouched(self) -> None:
        production = self.write_units_production()
        folder = self.vault_root / "showrunner"
        folder.mkdir(parents=True)
        handwritten = folder / "notes.md"
        _ = handwritten.write_text("kept by a person\n", encoding="utf-8")
        missing_production = folder / "missing-production.md"
        _ = missing_production.write_text(
            "---\nphase_table: true\nunit: table-unit\n---\nkept\n",
            encoding="utf-8",
        )
        missing_unit = folder / "missing-unit.md"
        _ = missing_unit.write_text(
            "---\nphase_table: true\nproduction: show\n---\nkept\n",
            encoding="utf-8",
        )
        other = self.write_phase_note(
            "showrunner", "other", "other", "table-unit"
        )
        before = {
            path: path.read_bytes()
            for path in (handwritten, missing_production, missing_unit, other)
        }

        result = self.run_archive(production)

        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertTrue(folder.is_dir())
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)

    def test_archive_keeps_existing_target_and_adds_numeric_suffix(self) -> None:
        production = self.write_units_production()
        source = self.write_phase_note("showrunner", "table", "show", "table-unit")
        source_bytes = source.read_bytes() + b"source\n"
        _ = source.write_bytes(source_bytes)
        target = (
            self.vault_root.parent
            / "archive"
            / "showrunner"
            / "show"
            / "table.md"
        )
        target.parent.mkdir(parents=True)
        existing_bytes = b"existing archive\n"
        _ = target.write_bytes(existing_bytes)

        result = self.run_archive(production)

        suffixed = target.with_name("table-2.md")
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertEqual(result.stdout, f"{suffixed}\n")
        self.assertEqual(target.read_bytes(), existing_bytes)
        self.assertEqual(suffixed.read_bytes(), source_bytes)
        self.assertFalse(source.exists())

    def test_archive_with_missing_vault_parent_does_nothing(self) -> None:
        production = self.write_units_production()
        missing_parent = self.root / "missing-vault"
        self.vault_root = missing_parent / "showrunners"

        result = self.run_archive(production)

        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertFalse(missing_parent.exists())

    def test_refresh_reuses_name_after_other_productions_note_is_archived(
        self,
    ) -> None:
        self.write_production_plan(register_showrunner=False)
        self.mark_unit("table")
        source = self.write_phase_note("showrunner", "table", "show", "table-unit")
        source_bytes = source.read_bytes()

        archive_result = self.run_archive(
            self.working_dir / "docs" / "show-production.md"
        )

        archived = (
            self.vault_root.parent
            / "archive"
            / "showrunner"
            / "show"
            / "table.md"
        )
        self.assertEqual(archive_result.returncode, 0)
        self.assertTrue(archived.is_file())
        self.assertFalse(source.exists())

        other_production = self.working_dir / "docs" / "other-production.md"
        _ = other_production.write_text(
            plan_text(
                "# Production — other",
                "",
                "## Production Context",
                "",
                "- **Merge branch:** main",
                f"- **Showrunner checkout:** {self.working_dir}",
                "- **Log:** docs/other.log",
                "- **User zone:** UTC",
                "",
                "## Units",
            ),
            encoding="utf-8",
        )
        self.write_plan(
            plan_text(
                "# Delivery plan",
                "",
                "> **Production: other** — unit `table-unit`; production doc `docs/other-production.md`",
                "",
                "### Phase 1 — Current delivery  · status: todo",
            )
        )
        fake_unit_host.write(self.environment(), [fake_unit_host.HostedUnit(
            "table-host", "w1:p1", {"SHOWRUNNER_UNIT": "other", "SHOWRUNNER_UNIT_ID": "table-unit"})])
        live_record = cast(dict[str, object], json.loads(
            (self.sessions_dir / f"record-{self.live_record_number}.json").read_text(encoding="utf-8")
        ))
        fake_unit_host.place_process(self.environment(), cast(int, live_record["pid"]), "w1:p1")
        session_id = "other-showrunner"
        _ = fake_showrunner.write_timer(
            self.notifier_dir,
            "other",
            session_id,
            "UTC",
            other_production,
        )
        held = fake_showrunner.write_session(
            self.sessions_dir,
            "showrunner",
            session_id,
        )
        self.addCleanup(held.close)

        refresh_result = self.run_refresh()

        refreshed = self.vault_root / "showrunner" / "table.md"
        self.assertEqual(
            (refresh_result.returncode, refresh_result.stdout, refresh_result.stderr),
            (0, "", ""),
        )
        refreshed_text = refreshed.read_text(encoding="utf-8")
        self.assertIn(f"{self.production_file_line('other')}\n", refreshed_text)
        self.assertIn("unit: table-unit\n", refreshed_text)
        self.assertEqual(archived.read_bytes(), source_bytes)

    def test_prune_leaves_archived_notes_in_place(self) -> None:
        production = self.write_units_production()
        _ = self.write_phase_note("showrunner", "retired", "show", "retired-unit")
        archive_result = self.run_archive(production)
        self.assertEqual(archive_result.returncode, 0)
        archived = (
            self.vault_root.parent
            / "archive"
            / "showrunner"
            / "show"
            / "retired.md"
        )
        archived_bytes = archived.read_bytes()

        prune_result = self.run_prune(production)

        self.assertEqual(
            (prune_result.returncode, prune_result.stdout, prune_result.stderr),
            (0, "", ""),
        )
        self.assertEqual(archived.read_bytes(), archived_bytes)

    def test_refresh_and_archive_keep_archive_visible_to_git(self) -> None:
        vault = self.vault_root.parent
        _ = subprocess.run(
            ["git", "init", "-q", str(vault)],
            check=True,
            capture_output=True,
        )
        self.write_production_plan()
        self.mark_unit("x")
        refresh_result = self.run_refresh()
        source = self.vault_root / "showrunner" / "x.md"
        archive_result = self.run_archive(
            self.working_dir / "docs" / "show-production.md"
        )
        archived = vault / "archive" / "showrunner" / "show" / "x.md"

        archived_ignore = subprocess.run(
            ["git", "-C", str(vault), "check-ignore", "archive/showrunner/show/x.md"],
            check=False,
            capture_output=True,
            text=True,
        )
        source_ignore = subprocess.run(
            ["git", "-C", str(vault), "check-ignore", "showrunners/showrunner/x.md"],
            check=False,
            capture_output=True,
            text=True,
        )
        gitignore = vault / ".gitignore"
        exclude = vault / ".git" / "info" / "exclude"

        self.assertEqual(refresh_result.returncode, 0)
        self.assertEqual(archive_result.returncode, 0)
        self.assertFalse(source.exists())
        self.assertTrue(archived.is_file())
        self.assertEqual(archived_ignore.returncode, 1)
        self.assertEqual(source_ignore.returncode, 0)
        self.assertNotIn(
            "archive",
            gitignore.read_text(encoding="utf-8") if gitignore.exists() else "",
        )
        self.assertNotIn("archive", exclude.read_text(encoding="utf-8"))

    def test_prune_archives_retired_notes_across_showrunner_directories(
        self,
    ) -> None:
        live_worktree = self.make_live_worktree("live-prune-worktree")
        live_plan = live_worktree / "plan.md"
        _ = live_plan.write_text(
            plan_text("### Phase 1 — Live  · status: todo"),
            encoding="utf-8",
        )
        production = self.write_units_production(
            ("live", "plan.md", live_worktree),
        )
        live = self.write_phase_note("current", "live", "show", "live")
        retired_one = self.write_phase_note(
            "old-one", "retired-one", "show", "retired-one"
        )
        retired_two = self.write_phase_note(
            "old-two", "retired-two", "show", "retired-two"
        )
        other_production = self.write_phase_note(
            "old-two", "other", "other-show", "retired-two"
        )
        handwritten = self.vault_root / "old-two" / "notes.md"
        _ = handwritten.write_text("kept by a person\n", encoding="utf-8")

        result = self.run_prune(production)

        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertTrue(live.is_file())
        self.assertFalse(retired_one.exists())
        self.assertFalse(retired_one.parent.exists())
        self.assertFalse(retired_two.exists())
        self.assertTrue(
            (
                self.vault_root.parent
                / "archive"
                / "old-one"
                / "show"
                / "retired-one.md"
            ).is_file()
        )
        self.assertTrue(
            (
                self.vault_root.parent
                / "archive"
                / "old-two"
                / "show"
                / "retired-two.md"
            ).is_file()
        )
        self.assertTrue(other_production.is_file())
        self.assertEqual(
            handwritten.read_text(encoding="utf-8"), "kept by a person\n"
        )

    def test_prune_with_only_retired_rows_archives_every_production_note(
        self,
    ) -> None:
        production = self.write_units_production(
            ("retired-one", "retired one.md", self.root / "gone-one"),
            ("retired-two", "retired two.md", self.root / "gone-two"),
        )
        first = self.write_phase_note(
            "showrunner-one", "first", "show", "retired-one"
        )
        second = self.write_phase_note(
            "showrunner-two", "second", "show", "retired-two"
        )

        result = self.run_prune(production)

        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())
        self.assertFalse(first.parent.exists())
        self.assertFalse(second.parent.exists())
        self.assertTrue(
            (
                self.vault_root.parent
                / "archive"
                / "showrunner-one"
                / "show"
                / "first.md"
            ).is_file()
        )
        self.assertTrue(
            (
                self.vault_root.parent
                / "archive"
                / "showrunner-two"
                / "show"
                / "second.md"
            ).is_file()
        )

    def test_prune_leaves_handwritten_notes_untouched(self) -> None:
        production = self.write_units_production()
        folder = self.vault_root / "showrunner"
        folder.mkdir(parents=True)
        plain = folder / "plain.md"
        _ = plain.write_text("personal status\n", encoding="utf-8")
        marked_without_owner = folder / "marked.md"
        _ = marked_without_owner.write_text(
            "---\nphase_table: true\n---\npersonal status\n",
            encoding="utf-8",
        )

        result = self.run_prune(production)

        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertEqual(plain.read_text(encoding="utf-8"), "personal status\n")
        self.assertIn(
            "personal status",
            marked_without_owner.read_text(encoding="utf-8"),
        )

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


class ProductionFileLinkTests(unittest.TestCase):
    """The `production_file` link names the doc's main-branch copy."""

    temporary: tempfile.TemporaryDirectory[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    vault: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.vault = self.root / "vault"
        self.vault.mkdir()

    def make_checkout(self, path: Path, remote: str) -> None:
        path.mkdir(parents=True)
        for arguments in (
            ("init", "--quiet", "--initial-branch=main"),
            ("remote", "add", "origin", remote),
        ):
            _ = subprocess.run(["git", "-C", str(path), *arguments], check=True)

    def write_doc(self, checkout: Path, relative: str, promote: str) -> Path:
        doc = checkout / relative
        doc.parent.mkdir(parents=True, exist_ok=True)
        _ = doc.write_text(f"# Production\n\n- **Promote:** {promote}\n", encoding="utf-8")
        return doc

    def test_doc_promoted_into_the_vault_is_a_wiki_link(self) -> None:
        trunk = self.root / "trunk"
        self.make_checkout(trunk, "git@github.com:someone/vault.git")
        doc = self.write_doc(trunk, "productions/notes-production.md", str(self.vault))

        link = phase_table._production_file_link(doc, self.vault)  # pyright: ignore[reportPrivateUsage]

        self.assertEqual(link, "[[productions/notes-production]]")

    def test_doc_promoted_outside_the_vault_is_its_github_page(self) -> None:
        main = self.root / "main"
        self.make_checkout(main, "git@github.com:someone/tools.git")
        _ = subprocess.run(
            ["git", "-C", str(main), "symbolic-ref", "HEAD", "refs/heads/trunk-line"],
            check=True,
        )
        trunk = self.root / "trunk"
        self.make_checkout(trunk, "git@github.com:someone/tools.git")
        doc = self.write_doc(trunk, "docs/plans/tools-production.md", f"{main}, mac ~/tools")

        link = phase_table._production_file_link(doc, self.vault)  # pyright: ignore[reportPrivateUsage]

        self.assertEqual(
            link,
            "https://github.com/someone/tools/blob/trunk-line/docs/plans/tools-production.md",
        )

    def test_doc_off_github_is_a_file_uri_of_the_main_copy(self) -> None:
        main = self.root / "main"
        self.make_checkout(main, str(self.root / "bare.git"))
        trunk = self.root / "trunk"
        self.make_checkout(trunk, str(self.root / "bare.git"))
        doc = self.write_doc(trunk, "docs/tools-production.md", str(main))

        link = phase_table._production_file_link(doc, self.vault)  # pyright: ignore[reportPrivateUsage]

        self.assertEqual(link, (main / "docs" / "tools-production.md").as_uri())

    def test_slug_is_read_from_each_link_form(self) -> None:
        cases = {
            '"[[productions/notes-production]]"': "notes",
            '"https://github.com/someone/tools/blob/main/docs/tools-production.md"': "tools",
            '"file:///home/someone/docs/tools-production.md"': "tools",
            '"[[productions/notes]]"': "",
            "": "",
        }
        for link, slug in cases.items():
            with self.subTest(link=link):
                self.assertEqual(
                    phase_table._production_slug_from_link(link),  # pyright: ignore[reportPrivateUsage]
                    slug,
                )

    def test_note_with_only_production_file_is_owned(self) -> None:
        note = self.root / "note.md"
        _ = note.write_text(
            "---\nphase_table: true\nshowrunner: natedev\n"
            + 'production_file: "[[productions/notes-production]]"\n'
            + "unit: alpha-unit\n---\n",
            encoding="utf-8",
        )

        self.assertEqual(
            phase_table._note_ownership(note),  # pyright: ignore[reportPrivateUsage]
            phase_table.NoteOwnership(production="notes", unit="alpha-unit"),
        )


if __name__ == "__main__":
    _ = unittest.main()
