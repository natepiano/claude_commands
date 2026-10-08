#!/usr/bin/env python3
"""Integration tests for the durable plan-delegate progress history."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

from scripts.delegate import progress_history


SCRIPT = Path(__file__).with_name("progress_history.py")
FINDINGS = Path(__file__).with_name("findings.py")
BOARD = Path(__file__).with_name("board.sh")
# Four reports per finished phase, as (raw percent, percent of the phase's time
# already spent): the shape of startup-polish Phase 5's history on 2026-10-03.
# Looked up at the exact number, each raw percent answers with its own time
# spent -- 78 says 40, 82 says 20, 84 says 55, 86 says 15 -- so four ticks a few
# points apart drew four ETAs hours apart.
SWING_REPORTS: tuple[tuple[int, int], ...] = ((86, 15), (82, 20), (78, 40), (84, 55))
SUMMARY_HEADER = ["Scope", "%", "Elapsed", "ETA", "Unchanged", "ETA low", "ETA high"]


class ProgressHistoryTests(unittest.TestCase):
    temporary: tempfile.TemporaryDirectory[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    history_dir: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    working_dir: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    config_file: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    agents_config_file: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    team_slot: str  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.history_dir = self.root / "history"
        self.working_dir = self.root / "bevy_hana_rubric"
        self.working_dir.mkdir()
        # Pin the report interval and the timezone: without both, the clock line
        # renders from the machine's own delegate.conf and local offset, and the
        # expected header would differ per machine.
        self.config_file = self.root / "delegate.conf"
        self.write_interval(180)
        # The temporary home's registry path. Named so the digest hashes a file
        # the test controls rather than the machine's own agents.conf, which
        # would differ per machine and per registry edit. write_agents_registry
        # writes to this same path: one file, so arm-review resolves the agent
        # the test wrote instead of reading an empty one beside it.
        self.team_slot = ""
        self.agents_config_file = self.root / ".claude" / "config" / "agents.conf"
        self.agents_config_file.parent.mkdir(parents=True, exist_ok=True)
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

    def write_interval(self, seconds: object) -> None:
        _ = self.config_file.write_text(
            f"PLAN_DELEGATE_PROGRESS_INTERVAL_SECONDS={seconds}\n",
            encoding="utf-8",
        )

    def isolate_identity(self, environment: dict[str, str]) -> None:
        """Point unit-director detection at the temporary home, not this machine's.

        Every window opening re-detects the unit director, and left alone that
        reads the real session transcript, so the recorded identity would be
        whichever model happens to be running the tests.
        """
        environment["HOME"] = str(self.root)
        _ = environment.pop("CODEX_THREAD_ID", None)
        _ = environment.pop("CLAUDE_CODE_SESSION_ID", None)

    def run_command(self, *arguments: str, at: int, claude_session: str = "") -> str:
        environment = os.environ.copy()
        environment["PLAN_DELEGATE_HISTORY_DIR"] = str(self.history_dir)
        environment["PLAN_DELEGATE_CONFIG"] = str(self.config_file)
        environment["AGENTS_CONFIG_FILE"] = str(self.agents_config_file)
        environment["TZ"] = "UTC"
        environment["PLAN_DELEGATE_NOW_EPOCH"] = str(at)
        environment["PLAN_DELEGATE_PASS_OWNER"] = "launcher"
        # Popped rather than left alone: the suite copies the ambient
        # environment, so a developer running with a seat exported would
        # otherwise see it stamped on every pass these tests record.
        if self.team_slot:
            environment["PLAN_DELEGATE_TEAM_ROLE"] = self.team_slot
        else:
            _ = environment.pop("PLAN_DELEGATE_TEAM_ROLE", None)
        self.isolate_identity(environment)
        if claude_session:
            environment["CLAUDE_CODE_SESSION_ID"] = claude_session
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
        environment["PLAN_DELEGATE_CONFIG"] = str(self.config_file)
        environment["AGENTS_CONFIG_FILE"] = str(self.agents_config_file)
        environment["TZ"] = "UTC"
        environment["PLAN_DELEGATE_NOW_EPOCH"] = str(at)
        environment["PLAN_DELEGATE_PASS_OWNER"] = "launcher"
        # Popped rather than left alone: the suite copies the ambient
        # environment, so a developer running with a seat exported would
        # otherwise see it stamped on every pass these tests record.
        if self.team_slot:
            environment["PLAN_DELEGATE_TEAM_ROLE"] = self.team_slot
        else:
            _ = environment.pop("PLAN_DELEGATE_TEAM_ROLE", None)
        self.isolate_identity(environment)
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
        plan_path = self.working_dir / "docs" / f"{name}.md"
        plan_path.parent.mkdir(exist_ok=True)
        plan_text = "\n".join(
            (
                "## Delegation Context",
                "",
                "- **Project:** Test project",
                f"- **Project started:** {datetime.fromtimestamp(started_at, UTC).isoformat()}",
                "",
            )
        )
        _ = plan_path.write_text(plan_text, encoding="utf-8")
        _ = self.run_command(
            "start-run",
            "--session-dir",
            str(session_dir),
            "--working-dir",
            str(self.working_dir),
            "--plan-doc",
            str(plan_path.relative_to(self.working_dir)),
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

    def seed_calibration_history(
        self,
        name: str,
        cap_stage: str,
        reports: tuple[tuple[int, int], ...],
        phases: int = 5,
    ) -> None:
        """Write finished phases straight into the history the calibrator reads.

        Each report is (raw percent, percent of the phase's time spent), made by
        the identity `start_phase_and_pass` records, so every one is a sample in
        the current run's narrowest scope. Driving hundreds of CLI calls to reach
        the same rows would only slow the suite.
        """
        duration = 10_000
        lines: list[str] = []
        for index in range(phases):
            started_at = 1_000_000 + index * duration
            phase: dict[str, object] = {
                "schema_version": 1,
                "phase_instance_id": f"{name}-{index}",
                "phase_started_at": started_at,
            }
            for raw_percent, spent_percent in reports:
                event: dict[str, object] = {
                    **phase,
                    "event_type": "progress_reported",
                    "timestamp_epoch": started_at + duration * spent_percent // 100,
                    "raw_percent": raw_percent,
                    "percent": raw_percent,
                    "cap_stage": cap_stage,
                    "pass_kind": "fix",
                    "main_agent": {"model": "gpt-main", "effort": "xhigh"},
                    "called_agent": {"model": "gpt-called", "effort": "high"},
                }
                lines.append(json.dumps(event))
            finished: dict[str, object] = {
                **phase,
                "event_type": "phase_finished",
                "status": "completed",
                "timestamp_epoch": started_at + duration,
            }
            lines.append(json.dumps(finished))
        path = self.history_dir / "runs" / f"{name}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def calibrate_and_report(
        self,
        session_dir: Path,
        raw_percent: int,
        cap_stage: str,
        at: int,
    ) -> tuple[int, str]:
        """Run one tick the way the report contract does: calibrate, then report
        the suggestion. Returns the suggestion and the phase row's ETA cell."""
        calibration_text = self.run_command(
            "calibrate",
            "--session-dir",
            str(session_dir),
            "--candidate-percent",
            str(raw_percent),
            "--cap-stage",
            cap_stage,
            at=at,
        )
        parsed: object = json.loads(calibration_text)  # pyright: ignore[reportAny]
        suggestion = cast(int, cast(dict[str, object], parsed)["suggested_percent"])
        header = self.run_command(
            "progress",
            "--session-dir",
            str(session_dir),
            "--project-raw-percent",
            "50",
            "--project-percent",
            "50",
            "--phase-raw-percent",
            str(raw_percent),
            "--phase-percent",
            str(suggestion),
            "--cap-stage",
            cap_stage,
            "--activity",
            "running the last gates",
            at=at,
        )
        return suggestion, self.table_rows(header, SUMMARY_HEADER)[1][3]

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
            "--cap-stage",
            "open_findings",
            "--activity",
            "correcting retry recovery",
            at=started_at + 100,
        )
        self.assertEqual(
            header.splitlines(),
            [
                "**bevy_hana_rubric - feature/rubric**",
                "",
                "| Scope   |   % |  Elapsed |         ETA | Unchanged |              ETA low |             ETA high |",
                "| ------- | --: | -------: | ----------: | --------- | -------------------: | -------------------: |",
                "| Project |  80 | 00:01:40 | today 05:35 |           | today 05:35 (-00:00) | today 05:35 (+00:00) |",
                "| Phase 3 |  25 | 00:01:40 | today 05:40 |           | today 05:36 (-00:03) | today 05:46 (+00:06) |",
                "",
                "**Phase 3: Retry handling**",
                "",
                "| Stage | Start    | Elapsed | Agent 1        | Agent 2 | Result  |",
                "| ----- | -------- | ------- | -------------- | ------- | ------- |",
                "| Fix 2 | 05:33:30 | 1m      | fix 1m running | -       | running |",
                "",
                "▸ **Fix 2 - correcting retry recovery**",
                "**now 1970-01-01 05:35:00 - next report 05:38:00**",
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
            "--cap-stage",
            "open_findings",
            "--activity",
            "correcting retry recovery",
            at=started_at + 160,
        )
        self.assertIn(
            "| Project |  80 | 00:02:40 | today 05:36 | 00:01:00  |",
            unchanged_header,
        )
        self.assertIn(
            "| Phase 3 |  25 | 00:02:40 | today 05:44 | 00:01:00  |",
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
            "--cap-stage",
            "open_findings",
            "--activity",
            "correcting retry recovery",
            at=started_at + 180,
        )
        self.assertIn(
            "| Project |  85 | 00:03:00 | today 05:36 |           |",
            independently_changed,
        )
        self.assertIn(
            "| Phase 3 |  25 | 00:03:00 | today 05:45 | 00:01:20  |",
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
                "**Phase 3: Retry handling - elapsed 00:01:40**",
                "**Fix 2 - correcting retry recovery - elapsed 00:01:30**",
                "**20% complete**",
                "**Total elapsed 00:01:40**",
                "**now 1970-01-01 05:35:00 - next report 05:38:00**",
            ],
        )

    def test_ad_hoc_run_reuses_plan_project_time_for_the_same_worktree_branch(
        self,
    ) -> None:
        planned_start = 1_000
        planned_session = self.start_run("planned", planned_start)
        _ = self.run_command(
            "finish-run",
            "--session-dir",
            str(planned_session),
            "--status",
            "stopped",
            at=1_100,
        )

        ad_hoc_start = 100_000
        ad_hoc_session = self.root / "ad-hoc"
        ad_hoc_session.mkdir()
        _ = self.run_command(
            "start-run",
            "--session-dir",
            str(ad_hoc_session),
            "--working-dir",
            str(self.working_dir),
            "--main-family",
            "codex",
            "--main-model",
            "gpt-main",
            "--main-effort",
            "xhigh",
            "--main-session-id",
            "main-ad-hoc",
            at=ad_hoc_start,
        )
        state_text = (ad_hoc_session / "progress_history_state.json").read_text(
            encoding="utf-8"
        )
        state_object: object = json.loads(state_text)  # pyright: ignore[reportAny]
        state = cast(dict[str, object], state_object)
        self.assertEqual(state["project_started_at"], float(planned_start))
        self.assertEqual(state["project_start_source"], "history_plan_field")
        self.assertEqual(state["plan_doc"], "")
        self.assertEqual(
            state["project_plan_doc"],
            str((self.working_dir / "docs" / "planned.md").resolve()),
        )

        del state["project_start_source"]
        del state["project_plan_doc"]
        state["project_started_at"] = float(ad_hoc_start)
        _ = (ad_hoc_session / "progress_history_state.json").write_text(
            json.dumps(state),
            encoding="utf-8",
        )

        self.start_phase_and_pass(ad_hoc_session, ad_hoc_start)
        header = self.run_command(
            "progress",
            "--session-dir",
            str(ad_hoc_session),
            "--project-raw-percent",
            "40",
            "--project-percent",
            "40",
            "--phase-raw-percent",
            "10",
            "--phase-percent",
            "10",
            "--cap-stage",
            "implementation",
            "--activity",
            "implementing",
            at=ad_hoc_start + 10,
        )
        self.assertIn("| Project |  40 | 1 day 03:30:10 ", header)
        self.assertIn("| Phase 3 |  10 |       00:00:10 |", header)

    def test_the_clock_line_names_the_armed_timer_then_falls_back_to_the_interval(
        self,
    ) -> None:
        started_at = 20_000
        session_dir = self.start_run("clock", started_at)
        self.start_phase_and_pass(session_dir, started_at)

        # A timer armed and still ahead of the clock is the real next tick, so
        # its deadline wins over the interval added to the current time.
        _ = (session_dir / "progress_timer").write_text(
            f"deadline_epoch={started_at + 400}\npid=1234\ninterval_seconds=600\n",
            encoding="utf-8",
        )
        armed = self.run_progress(session_dir, at=started_at + 100)
        self.assertIn("**now 1970-01-01 05:35:00 - next report 05:40:00**", armed)

        # progress_timer.sh clears the marker as it ticks, and an expired one
        # left behind names a tick that has already happened. Both fall through
        # to the configured interval.
        _ = (session_dir / "progress_timer").write_text(
            f"deadline_epoch={started_at + 50}\npid=1234\ninterval_seconds=600\n",
            encoding="utf-8",
        )
        expired = self.run_progress(session_dir, at=started_at + 100)
        self.assertIn("**now 1970-01-01 05:35:00 - next report 05:38:00**", expired)

        (session_dir / "progress_timer").unlink()
        self.write_interval(90_000)
        crosses_midnight = self.run_progress(session_dir, at=started_at + 100)
        self.assertIn(
            "**now 1970-01-01 05:35:00 - next report 1970-01-02 06:35:00**",
            crosses_midnight,
        )

        # An unusable interval degrades the clause rather than stopping the
        # header: the report is not the timer, and progress_timer.sh is the
        # caller that fails loudly on the same key.
        self.write_interval("not-a-number")
        unusable = self.run_progress(session_dir, at=started_at + 100)
        self.assertIn("**now 1970-01-01 05:35:00**", unusable)
        self.assertNotIn("next report", unusable)

    def test_the_clock_line_uses_the_restarted_unit_notifier(self) -> None:
        started_at = 20_000
        report_at = started_at + 100
        session_dir = self.start_run("notifier-clock", started_at)
        self.start_phase_and_pass(session_dir, started_at)
        state_dir = self.root / "notifier"
        environment = os.environ.copy()
        environment["NOTIFIER_STATE_DIR"] = str(state_dir)
        environment["NOTIFIER_NOW_EPOCH"] = str(started_at)
        environment["TZ"] = "UTC"
        _ = subprocess.run(
            [
                "zsh",
                str(SCRIPT.parents[1] / "message" / "notifier.sh"),
                "new",
                f"delegate-{session_dir.name}",
                "--to",
                "session:test-claude-session",
                "--every",
                "15",
                "--command",
                "/unit:report",
            ],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        # A legacy marker must yield to the live instance's new deadline.
        _ = (session_dir / "progress_timer").write_text(
            f"deadline_epoch={started_at + 400}\npid=1234\ninterval_seconds=600\n",
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"NOTIFIER_STATE_DIR": str(state_dir)}):
            report = self.run_progress(session_dir, at=report_at)
        self.assertIn("**now 1970-01-01 05:35:00 - next report 05:50:00**", report)
        state = dict(
            line.split("=", 1)
            for line in (state_dir / f"delegate-{session_dir.name}" / "state")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        self.assertEqual(state["LAST_RESTART"], str(report_at))
        self.assertEqual(state["NEXT_DUE"], str(report_at + 15 * 60))

    def test_missing_cap_stage_restarts_unit_notifier(self) -> None:
        started_at = 50_000
        report_at = started_at + 100
        session_dir = self.start_run("refused-cap-stage", started_at)
        self.start_phase_and_pass(session_dir, started_at)
        state_dir = self.root / "notifier"
        environment = os.environ.copy()
        environment["NOTIFIER_STATE_DIR"] = str(state_dir)
        environment["NOTIFIER_NOW_EPOCH"] = str(started_at)
        environment["TZ"] = "UTC"
        _ = subprocess.run(
            [
                "zsh",
                str(SCRIPT.parents[1] / "message" / "notifier.sh"),
                "new",
                f"delegate-{session_dir.name}",
                "--to",
                "session:test-claude-session",
                "--every",
                "15",
                "--command",
                "/unit:report",
            ],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        with patch.dict(
            os.environ,
            {"NOTIFIER_STATE_DIR": str(state_dir), "NOTIFIER_NOW_EPOCH": str(report_at)},
        ):
            result = self.run_failing_command(
                "progress",
                "--session-dir",
                str(session_dir),
                "--project-raw-percent",
                "40",
                "--project-percent",
                "40",
                "--phase-raw-percent",
                "40",
                "--phase-percent",
                "40",
                "--activity",
                "implementing",
                at=report_at,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--cap-stage is required", result.stderr)
        state = dict(
            line.split("=", 1)
            for line in (state_dir / f"delegate-{session_dir.name}" / "state")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        self.assertEqual(state["LAST_RESTART"], str(report_at))
        self.assertEqual(state["NEXT_DUE"], str(report_at + 15 * 60))

    def test_a_finished_phase_is_refused_and_restarts_unit_notifier(self) -> None:
        """With no active phase there is nothing to report, and the clock still moves."""
        started_at = 20_000
        report_at = started_at + 100
        session_dir = self.start_run("refused-phase", started_at)
        self.start_phase_and_pass(session_dir, started_at)
        _ = self.run_command(
            "finish-phase",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed",
            at=started_at + 60,
        )
        state_dir = self.root / "notifier"
        environment = os.environ.copy()
        environment["NOTIFIER_STATE_DIR"] = str(state_dir)
        environment["NOTIFIER_NOW_EPOCH"] = str(started_at)
        environment["TZ"] = "UTC"
        _ = subprocess.run(
            [
                "zsh",
                str(SCRIPT.parents[1] / "message" / "notifier.sh"),
                "new",
                f"delegate-{session_dir.name}",
                "--to",
                "session:test-claude-session",
                "--every",
                "15",
                "--command",
                "/unit:report",
            ],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        with patch.dict(
            os.environ,
            {"NOTIFIER_STATE_DIR": str(state_dir), "NOTIFIER_NOW_EPOCH": str(report_at)},
        ):
            result = self.run_failing_command(
                "progress",
                "--session-dir",
                str(session_dir),
                "--activity",
                "implementing",
                at=report_at,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No active phase to report: phase completed.", result.stderr)
        self.assertIn("say nothing about whether the workers are alive", result.stderr)
        state = dict(
            line.split("=", 1)
            for line in (state_dir / f"delegate-{session_dir.name}" / "state")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        self.assertEqual(state["LAST_RESTART"], str(report_at))
        self.assertEqual(state["NEXT_DUE"], str(report_at + 15 * 60))

    def test_a_closed_phase_with_a_running_activity_reports_its_last_tables(
        self,
    ) -> None:
        started_at = 20_000
        session_dir = self.start_run("closed-phase-progress", started_at)
        self.start_phase_and_pass(session_dir, started_at)
        _ = self.run_progress(session_dir, at=started_at + 100)
        self.close_the_only_pass(session_dir, at=started_at + 200)
        _ = self.run_command(
            "finish-phase",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed",
            at=started_at + 300,
        )
        _ = self.run_command(
            "start-activity",
            "--session-dir",
            str(session_dir),
            "--label",
            "shrink",
            "--activity",
            "recording the as-built phase",
            at=started_at + 310,
        )
        calibration_text = self.run_command(
            "calibrate",
            "--session-dir",
            str(session_dir),
            "--candidate-percent",
            "65",
            at=started_at + 320,
        )
        parsed: object = json.loads(calibration_text)  # pyright: ignore[reportAny]
        calibration = cast(dict[str, object], parsed)
        self.assertEqual(calibration["candidate_percent"], 65)

        history = self.history_dir / "runs" / "closed-phase-progress.jsonl"
        state_path = session_dir / "progress_history_state.json"
        events_before = history.read_text(encoding="utf-8")
        state_before = state_path.read_text(encoding="utf-8")
        report = self.run_command(
            "progress",
            "--session-dir",
            str(session_dir),
            "--project-raw-percent",
            "99",
            "--project-percent",
            "99",
            "--phase-raw-percent",
            "99",
            "--phase-percent",
            "99",
            "--cap-stage",
            "complete",
            "--activity",
            "this report argument must be ignored",
            at=started_at + 400,
        )

        self.assertEqual(
            report.splitlines()[:3],
            [
                "**bevy_hana_rubric - feature/rubric**",
                "",
                "*Percentages and clocks as of 05:35:00, the last progress report.*",
            ],
        )
        self.assertEqual(
            [row[:3] for row in self.table_rows(report, SUMMARY_HEADER)[:2]],
            [["Project", "40", "00:01:40"], ["Phase 3", "30", "00:01:40"]],
        )
        rounds = self.table_rows(
            report,
            ["Stage", "Start", "Elapsed", "Agent 1", "Agent 2", "Result"],
        )
        self.assertEqual([row[0] for row in rounds], ["Fix 2", "shrink"])
        self.assertEqual(rounds[-1][-1], "running")
        self.assertIn("recording the as-built phase", report)
        self.assertNotIn("this report argument must be ignored", report)
        self.assertNotIn("No pass or activity is open.", report)
        self.assertEqual(
            report.splitlines()[-1],
            "**now 1970-01-01 05:40:00 - next report 05:43:00**",
        )
        self.assertEqual(history.read_text(encoding="utf-8"), events_before)
        self.assertEqual(state_path.read_text(encoding="utf-8"), state_before)
        progress_events = [
            event
            for event in self.read_events("closed-phase-progress")
            if event.get("event_type") == "progress_reported"
        ]
        self.assertEqual(len(progress_events), 1)
        state = self.read_state(session_dir)
        self.assertEqual(state["pending_calibration"], calibration)

    def test_a_closed_phase_with_a_finished_activity_has_no_active_report(
        self,
    ) -> None:
        started_at = 21_000
        session_dir = self.start_run("closed-finished-activity", started_at)
        self.start_phase(session_dir, started_at + 10)
        _ = self.run_command(
            "finish-phase",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed",
            at=started_at + 100,
        )
        _ = self.run_command(
            "start-activity",
            "--session-dir",
            str(session_dir),
            "--label",
            "shrink",
            "--activity",
            "recording the as-built phase",
            at=started_at + 110,
        )
        _ = self.run_command(
            "finish-activity",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed",
            "--result",
            "pass",
            at=started_at + 140,
        )

        result = self.run_failing_command(
            "progress",
            "--session-dir",
            str(session_dir),
            "--activity",
            "reporting after shrink",
            at=started_at + 150,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No active phase to report: phase completed.", result.stderr)

    def test_an_active_phase_progress_report_keeps_its_exact_output(self) -> None:
        started_at = 20_000
        session_dir = self.start_run("active-phase-output", started_at)
        self.start_phase_and_pass(session_dir, started_at)

        report = self.run_progress(session_dir, at=started_at + 100)

        self.assertEqual(
            report,
            "\n".join(
                (
                    "**bevy_hana_rubric - feature/rubric**",
                    "",
                    "| Scope   |   % |  Elapsed |         ETA | Unchanged |              ETA low |             ETA high |",
                    "| ------- | --: | -------: | ----------: | --------- | -------------------: | -------------------: |",
                    "| Project |  40 | 00:01:40 | today 05:37 |           | today 05:36 (-00:00) | today 05:38 (+00:01) |",
                    "| Phase 3 |  30 | 00:01:40 | today 05:38 |           | today 05:37 (-00:01) | today 05:41 (+00:02) |",
                    "",
                    "**Phase 3: Retry handling**",
                    "",
                    "| Stage | Start    | Elapsed | Agent 1        | Agent 2 | Result  |",
                    "| ----- | -------- | ------- | -------------- | ------- | ------- |",
                    "| Fix 2 | 05:33:30 | 1m      | fix 1m running | -       | running |",
                    "",
                    "▸ **Fix 2 - implementing**",
                    "**now 1970-01-01 05:35:00 - next report 05:38:00**",
                )
            ),
        )

    def test_an_activity_after_a_closed_phase_keeps_the_phase_record(self) -> None:
        started_at = 21_000
        session_dir = self.start_run("closed-phase-activity", started_at)
        self.start_phase(session_dir, started_at + 10)
        _ = self.run_command(
            "finish-phase",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed",
            at=started_at + 100,
        )
        phase_before = cast(dict[str, object], self.read_state(session_dir)["phase"])
        phase_instance_id = cast(str, phase_before["instance_id"])

        _ = self.run_command(
            "start-activity",
            "--session-dir",
            str(session_dir),
            "--label",
            "Shrink",
            "--activity",
            "recording the as-built phase",
            at=started_at + 110,
        )
        _ = self.run_command(
            "finish-activity",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed",
            "--result",
            "pass",
            at=started_at + 140,
        )

        phase_after = cast(dict[str, object], self.read_state(session_dir)["phase"])
        self.assertEqual(phase_after, phase_before)
        self.assertEqual(phase_after["status"], "completed")
        self.assertEqual(phase_after["finished_at"], float(started_at + 100))
        phase_finished_events = [
            event
            for event in self.read_events("closed-phase-activity")
            if event.get("event_type") == "phase_finished"
        ]
        self.assertEqual(len(phase_finished_events), 1)
        self.assertEqual(phase_finished_events[0].get("phase_elapsed_seconds"), 90)
        activity_events = [
            event
            for event in self.read_events("closed-phase-activity")
            if event.get("event_type") in {"activity_started", "activity_finished"}
        ]
        self.assertEqual(
            [event.get("event_type") for event in activity_events],
            ["activity_started", "activity_finished"],
        )
        self.assertEqual(
            {event.get("phase_instance_id") for event in activity_events},
            {phase_instance_id},
        )
        self.assertEqual(activity_events[-1].get("status"), "completed")
        self.assertEqual(activity_events[-1].get("activity_result"), "pass")
        self.assertEqual(activity_events[-1].get("activity_elapsed_seconds"), 30)

    def test_repeating_finish_phase_keeps_a_closed_phase_activity_open(self) -> None:
        started_at = 21_500
        session_dir = self.start_run("repeated-phase-finish", started_at)
        self.start_phase(session_dir, started_at + 10)
        _ = self.run_command(
            "finish-phase",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed",
            at=started_at + 100,
        )
        phase_before = cast(dict[str, object], self.read_state(session_dir)["phase"])
        _ = self.run_command(
            "start-activity",
            "--session-dir",
            str(session_dir),
            "--label",
            "Shrink",
            "--activity",
            "recording the as-built phase",
            at=started_at + 110,
        )
        _ = self.run_command(
            "finish-phase",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed",
            at=started_at + 120,
        )
        phase_after = cast(dict[str, object], self.read_state(session_dir)["phase"])
        self.assertEqual(phase_after["status"], phase_before["status"])
        self.assertEqual(phase_after["finished_at"], phase_before["finished_at"])
        _ = self.run_command(
            "finish-activity",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed",
            "--result",
            "pass",
            at=started_at + 150,
        )

        events = self.read_events("repeated-phase-finish")
        phase_finished_events = [
            event for event in events if event.get("event_type") == "phase_finished"
        ]
        self.assertEqual(len(phase_finished_events), 1)
        activity_finished_events = [
            event for event in events if event.get("event_type") == "activity_finished"
        ]
        self.assertEqual(len(activity_finished_events), 1)
        self.assertEqual(activity_finished_events[0].get("status"), "completed")
        self.assertEqual(activity_finished_events[0].get("activity_result"), "pass")
        self.assertEqual(
            activity_finished_events[0].get("activity_elapsed_seconds"), 40
        )

    def test_start_activity_distinguishes_missing_phase_from_finished_run(self) -> None:
        started_at = 22_000
        no_phase = self.start_run("activity-without-phase", started_at)
        refused = self.run_failing_command(
            "start-activity",
            "--session-dir",
            str(no_phase),
            "--label",
            "Verification",
            "--activity",
            "running checks",
            at=started_at + 10,
        )
        self.assertEqual(refused.returncode, 1)
        self.assertIn("Start a phase before starting an activity", refused.stderr)

        finished = self.start_run("activity-after-run", started_at + 100)
        self.start_phase(finished, started_at + 110)
        _ = self.run_command(
            "finish-phase",
            "--session-dir",
            str(finished),
            "--status",
            "completed",
            at=started_at + 120,
        )
        _ = self.run_command(
            "finish-run",
            "--session-dir",
            str(finished),
            "--status",
            "completed",
            at=started_at + 130,
        )
        refused = self.run_failing_command(
            "start-activity",
            "--session-dir",
            str(finished),
            "--label",
            "Verification",
            "--activity",
            "running checks",
            at=started_at + 140,
        )
        self.assertEqual(refused.returncode, 1)
        self.assertIn("run is finished", refused.stderr.lower())

    def test_a_closed_phase_activity_is_interrupted_by_the_next_phase_or_run_end(
        self,
    ) -> None:
        for index, ending in enumerate(("next phase", "run end")):
            with self.subTest(ending=ending):
                started_at = 23_000 + index * 1_000
                name = f"closed-activity-{index}"
                session_dir = self.start_run(name, started_at)
                self.start_phase(session_dir, started_at + 10)
                phase = cast(dict[str, object], self.read_state(session_dir)["phase"])
                phase_instance_id = cast(str, phase["instance_id"])
                _ = self.run_command(
                    "finish-phase",
                    "--session-dir",
                    str(session_dir),
                    "--status",
                    "completed",
                    at=started_at + 100,
                )
                _ = self.run_command(
                    "start-activity",
                    "--session-dir",
                    str(session_dir),
                    "--label",
                    "Shrink",
                    "--activity",
                    "recording the completed phase",
                    at=started_at + 110,
                )
                if ending == "next phase":
                    _ = self.run_command(
                        "start-phase",
                        "--session-dir",
                        str(session_dir),
                        "--phase-id",
                        "4",
                        "--phase-title",
                        "Final checks",
                        at=started_at + 140,
                    )
                else:
                    _ = self.run_command(
                        "finish-run",
                        "--session-dir",
                        str(session_dir),
                        "--status",
                        "completed",
                        at=started_at + 140,
                    )

                finished_events = [
                    event
                    for event in self.read_events(name)
                    if event.get("event_type") == "activity_finished"
                ]
                self.assertEqual(len(finished_events), 1)
                self.assertEqual(
                    finished_events[0].get("phase_instance_id"), phase_instance_id
                )
                self.assertEqual(finished_events[0].get("status"), "interrupted")
                self.assertEqual(finished_events[0].get("activity_result"), "")
                self.assertEqual(
                    finished_events[0].get("activity_elapsed_seconds"), 30
                )

    def close_the_only_pass(self, session_dir: Path, at: int) -> None:
        """Finish the pass `start_phase_and_pass` opened: reviews closed, no writer yet."""
        _ = self.run_command(
            "finish-pass", "--session-dir", str(session_dir), "--status", "completed", at=at
        )

    def test_a_phase_between_windows_reports_its_last_recorded_tables(self) -> None:
        """No window open is a moment inside the phase, not the end of the report."""
        started_at = 20_000
        session_dir = self.start_run("between-windows", started_at)
        self.start_phase_and_pass(session_dir, started_at)
        _ = self.run_progress(session_dir, at=started_at + 100)
        self.close_the_only_pass(session_dir, at=started_at + 200)
        state_dir = self.root / "notifier"
        environment = os.environ.copy()
        environment["NOTIFIER_STATE_DIR"] = str(state_dir)
        environment["NOTIFIER_NOW_EPOCH"] = str(started_at)
        environment["TZ"] = "UTC"
        _ = subprocess.run(
            [
                "zsh",
                str(SCRIPT.parents[1] / "message" / "notifier.sh"),
                "new",
                f"delegate-{session_dir.name}",
                "--to",
                "session:test-claude-session",
                "--every",
                "15",
                "--command",
                "/unit:report",
            ],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        report_at = started_at + 400
        with patch.dict(
            os.environ,
            {"NOTIFIER_STATE_DIR": str(state_dir), "NOTIFIER_NOW_EPOCH": str(report_at)},
        ):
            # Percents the tick passes are neither required nor checked: the
            # report shows what was recorded, not what this call claims.
            report = self.run_command(
                "progress",
                "--session-dir",
                str(session_dir),
                "--phase-raw-percent",
                "55",
                "--activity",
                "waiting on the repair writers",
                at=report_at,
            )
        lines = report.splitlines()
        self.assertEqual(
            lines[:3],
            [
                "**bevy_hana_rubric - feature/rubric**",
                "",
                "*Percentages and clocks as of 05:35:00, the last progress report.*",
            ],
        )
        summary = self.table_rows(report, SUMMARY_HEADER)
        # The rows the 05:35:00 report printed: its percents and its clocks.
        self.assertEqual(
            [row[:3] for row in summary[:2]],
            [["Project", "40", "00:01:40"], ["Phase 3", "30", "00:01:40"]],
        )
        rounds = self.table_rows(
            report, ["Stage", "Start", "Elapsed", "Agent 1", "Agent 2", "Result"]
        )
        self.assertEqual(
            [(row[0], row[3], row[5]) for row in rounds],
            [("Fix 2", "fix 3m done", "done")],
        )
        warning = lines[-2]
        self.assertTrue(warning.startswith("No pass or activity is open. "))
        self.assertIn("say nothing about whether the workers are alive", warning)
        self.assertGreater(lines.index(warning), lines.index(next(
            line for line in lines if line.startswith("| Fix 2 ")
        )))
        self.assertEqual(lines[-1], "**now 1970-01-01 05:40:00 - next report 05:55:00**")
        restart = dict(
            line.split("=", 1)
            for line in (state_dir / f"delegate-{session_dir.name}" / "state")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        self.assertEqual(restart["LAST_RESTART"], str(report_at))

    def test_a_phase_never_reported_still_prints_both_tables(self) -> None:
        """Before the first report the clocks read as of the phase start."""
        started_at = 20_000
        session_dir = self.start_run("never-reported", started_at)
        self.start_phase_and_pass(session_dir, started_at)
        self.close_the_only_pass(session_dir, at=started_at + 200)
        report = self.run_command(
            "progress",
            "--session-dir",
            str(session_dir),
            "--activity",
            "waiting on the repair writers",
            at=started_at + 400,
        )
        self.assertIn(
            "*Percentages and clocks as of 05:33:20, the phase start, before any "
            + "progress report.*",
            report,
        )
        self.assertEqual(
            [row[:3] for row in self.table_rows(report, SUMMARY_HEADER)[:2]],
            [["Project", "0", "00:00:00"], ["Phase 3", "0", "00:00:00"]],
        )
        self.assertEqual(
            [
                row[0]
                for row in self.table_rows(
                    report, ["Stage", "Start", "Elapsed", "Agent 1", "Agent 2", "Result"]
                )
            ],
            ["Fix 2"],
        )

    def test_a_phase_with_no_window_yet_draws_a_row_saying_so(self) -> None:
        started_at = 20_000
        session_dir = self.start_run("no-window-yet", started_at)
        self.start_phase(session_dir, started_at)
        report = self.run_command(
            "progress", "--session-dir", str(session_dir), "--activity", "starting", at=started_at + 60
        )
        self.assertEqual(
            self.table_rows(report, ["Stage", "Start", "Elapsed", "Agent 1", "Agent 2", "Result"]),
            [["-", "-", "-", "-", "-", "no stage yet"]],
        )

    def test_a_report_between_windows_records_nothing(self) -> None:
        """No event, no window: pass counts and convergence never see the call."""
        started_at = 20_000
        session_dir = self.start_run("records-nothing", started_at)
        self.start_phase_and_pass(session_dir, started_at)
        _ = self.run_progress(session_dir, at=started_at + 100)
        self.close_the_only_pass(session_dir, at=started_at + 200)
        history = self.history_dir / "runs" / "records-nothing.jsonl"
        state_path = session_dir / "progress_history_state.json"
        events_before = history.read_text(encoding="utf-8")
        state_before = state_path.read_text(encoding="utf-8")
        _ = self.run_progress(session_dir, at=started_at + 400)
        self.assertEqual(history.read_text(encoding="utf-8"), events_before)
        self.assertEqual(state_path.read_text(encoding="utf-8"), state_before)
        self.assertEqual(
            {str(record["status"]) for record in self.pass_slots(session_dir).values()},
            {"completed"},
        )

    def report_phase_percent(self, session_dir: Path, percent: int, at: int) -> None:
        _ = self.run_command(
            "progress",
            "--session-dir",
            str(session_dir),
            "--project-raw-percent",
            "40",
            "--project-percent",
            "40",
            "--phase-raw-percent",
            str(percent),
            "--phase-percent",
            str(percent),
            "--cap-stage",
            "implementation",
            "--activity",
            "implementing",
            at=at,
        )

    def test_a_report_between_windows_names_the_eta_day_from_now(self) -> None:
        """Recorded 23:30 on the 3rd, read at 03:00 on the 4th: 00:40 is today."""
        recorded_at = 2 * 86_400 + 23 * 3_600 + 1_800
        session_dir = self.start_run("past-midnight", recorded_at - 1_800)
        self.start_phase_and_pass(session_dir, recorded_at - 1_800)
        self.report_phase_percent(session_dir, 30, at=recorded_at)
        self.close_the_only_pass(session_dir, at=recorded_at + 60)
        report = self.run_progress(session_dir, at=3 * 86_400 + 3 * 3_600)
        self.assertIn("as of 1970-01-03 23:30:00, the last progress report", report)
        phase_row = self.table_rows(report, SUMMARY_HEADER)[1]
        self.assertEqual(phase_row[:4], ["Phase 3", "30", "00:30:00", "today 00:40"])
        self.assertTrue(phase_row[5].startswith("today "))
        self.assertTrue(phase_row[6].startswith("today "))

    def test_a_report_between_windows_reads_the_latest_report_of_the_phase(self) -> None:
        started_at = 20_000
        session_dir = self.start_run("latest-report", started_at)
        self.start_phase_and_pass(session_dir, started_at)
        self.report_phase_percent(session_dir, 20, at=started_at + 100)
        self.report_phase_percent(session_dir, 50, at=started_at + 150)
        self.close_the_only_pass(session_dir, at=started_at + 200)
        report = self.run_progress(session_dir, at=started_at + 400)
        self.assertIn("as of 05:35:50, the last progress report", report)
        self.assertEqual(self.table_rows(report, SUMMARY_HEADER)[1][:2], ["Phase 3", "50"])

    def test_a_report_between_windows_ignores_an_earlier_phase(self) -> None:
        started_at = 20_000
        session_dir = self.start_run("earlier-phase", started_at)
        self.start_phase_and_pass(session_dir, started_at)
        self.report_phase_percent(session_dir, 30, at=started_at + 100)
        self.close_the_only_pass(session_dir, at=started_at + 200)
        _ = self.run_command(
            "finish-phase", "--session-dir", str(session_dir), "--status", "completed",
            at=started_at + 300,
        )
        _ = self.run_command(
            "start-phase", "--session-dir", str(session_dir), "--phase-id", "4",
            "--phase-title", "Next phase", at=started_at + 400,
        )
        report = self.run_progress(session_dir, at=started_at + 500)
        self.assertIn("the phase start, before any progress report", report)
        self.assertEqual(self.table_rows(report, SUMMARY_HEADER)[1][:2], ["Phase 4", "0"])

    def test_a_run_with_no_phase_is_refused(self) -> None:
        session_dir = self.start_run("no-phase", 20_000)
        result = self.run_failing_command(
            "progress", "--session-dir", str(session_dir), "--activity", "idle", at=20_100
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No active phase to report: phase missing.", result.stderr)

    def run_progress(self, session_dir: Path, at: int) -> str:
        return self.run_command(
            "progress",
            "--session-dir",
            str(session_dir),
            "--project-raw-percent",
            "40",
            "--project-percent",
            "40",
            "--phase-raw-percent",
            "30",
            "--phase-percent",
            "30",
            "--cap-stage",
            "implementation",
            "--activity",
            "implementing",
            at=at,
        )

    def test_start_run_persists_the_plan_git_time_without_an_agent_timestamp(
        self,
    ) -> None:
        plan_path = self.working_dir / "docs" / "derived.md"
        plan_path.parent.mkdir(exist_ok=True)
        _ = plan_path.write_text(
            "## Delegation Context\n\n- **Project:** Derived project\n",
            encoding="utf-8",
        )
        commit_time = 1_700_000_000
        commit_time_text = datetime.fromtimestamp(commit_time, UTC).isoformat()
        environment = os.environ.copy()
        environment["GIT_AUTHOR_DATE"] = commit_time_text
        environment["GIT_COMMITTER_DATE"] = commit_time_text
        _ = subprocess.run(
            ["git", "add", str(plan_path.relative_to(self.working_dir))],
            cwd=self.working_dir,
            check=True,
            capture_output=True,
            text=True,
        )
        _ = subprocess.run(
            [
                "git",
                "-c",
                "user.name=Plan Delegate Test",
                "-c",
                "user.email=plan-delegate@example.invalid",
                "commit",
                "-m",
                "checkpoint(derived): phase 1",
            ],
            cwd=self.working_dir,
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )

        session_dir = self.root / "derived"
        session_dir.mkdir()
        _ = self.run_command(
            "start-run",
            "--session-dir",
            str(session_dir),
            "--working-dir",
            str(self.working_dir),
            "--plan-doc",
            str(plan_path.relative_to(self.working_dir)),
            "--main-family",
            "codex",
            "--main-model",
            "gpt-main",
            "--main-effort",
            "xhigh",
            "--main-session-id",
            "main-derived",
            at=commit_time + 1_000,
        )

        state_text = (session_dir / "progress_history_state.json").read_text(
            encoding="utf-8"
        )
        state_object: object = json.loads(state_text)  # pyright: ignore[reportAny]
        state = cast(dict[str, object], state_object)
        self.assertEqual(state["project_started_at"], float(commit_time))
        self.assertEqual(state["project_start_source"], "plan_first_checkpoint")
        persisted_text = plan_path.read_text(encoding="utf-8")
        persisted_value = persisted_text.split(
            "- **Project started:** ",
            maxsplit=1,
        )[1].strip()
        self.assertEqual(
            datetime.fromisoformat(persisted_value).timestamp(),
            commit_time,
        )

    def test_long_elapsed_times_split_days_from_clock_hours(self) -> None:
        started_at = 30_000
        session_dir = self.start_run("long-duration", started_at)
        self.start_phase_and_pass(session_dir, started_at)

        first = self.run_command(
            "progress",
            "--session-dir",
            str(session_dir),
            "--raw-percent",
            "20",
            "--percent",
            "20",
            "--activity",
            "implementing",
            at=started_at + 90_061,
        )
        self.assertIn("elapsed 1 day 01:01:01", first)

        second = self.run_command(
            "progress",
            "--session-dir",
            str(session_dir),
            "--raw-percent",
            "20",
            "--percent",
            "20",
            "--activity",
            "implementing",
            at=started_at + 176_461,
        )
        self.assertIn("elapsed 2 days 01:01:01", second)
        self.assertIn("unchanged for 1 day 00:00:00", second)

    def test_cap_stage_clamps_optimistic_estimates(self) -> None:
        started_at = 40_000
        session_dir = self.start_run("capped", started_at)
        self.start_phase_and_pass(session_dir, started_at)
        header = self.run_command(
            "progress",
            "--session-dir",
            str(session_dir),
            "--project-raw-percent",
            "100",
            "--project-percent",
            "100",
            "--phase-raw-percent",
            "99",
            "--phase-percent",
            "99",
            "--activity",
            "waiting on the closure review",
            "--cap-stage",
            "open_findings",
            at=started_at + 100,
        )
        self.assertIn("| Phase 3 |  90 |", header)
        self.assertIn("| Project |  99 |", header)
        self.assertNotIn("| Phase 3 | 100 |", header)
        self.assertNotIn("| Project | 100 |", header)

        history_text = (self.history_dir / "runs" / "capped.jsonl").read_text(encoding="utf-8")
        self.assertIn('"cap_stage":"open_findings"', history_text)
        self.assertIn('"phase_uncapped_percent":99', history_text)
        self.assertIn('"phase_percent_capped_by":"open_findings"', history_text)
        self.assertIn('"project_uncapped_percent":100', history_text)

    def test_dual_layout_progress_requires_a_cap_stage(self) -> None:
        started_at = 50_000
        session_dir = self.start_run("uncapped", started_at)
        self.start_phase_and_pass(session_dir, started_at)
        result = self.run_failing_command(
            "progress",
            "--session-dir",
            str(session_dir),
            "--project-raw-percent",
            "40",
            "--project-percent",
            "40",
            "--phase-raw-percent",
            "40",
            "--phase-percent",
            "40",
            "--activity",
            "implementing",
            at=started_at + 60,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--cap-stage is required", result.stderr)

    def test_start_phase_records_work_order_size(self) -> None:
        started_at = 60_000
        session_dir = self.start_run("sized", started_at)
        work_order = self.root / "work_order.md"
        work_order_lines = [
            "**Goal:** wire the retry path",
            "",
            "- touch `src/retry.rs`",
            "- touch `src/lib.rs`",
            "- keep `RetryBudget` intact",
        ]
        _ = work_order.write_text(
            "\n".join(work_order_lines) + "\n",
            encoding="utf-8",
        )
        _ = self.run_command(
            "start-phase",
            "--session-dir",
            str(session_dir),
            "--phase-id",
            "3",
            "--phase-title",
            "Retry handling",
            "--work-order-file",
            str(work_order),
            at=started_at,
        )
        history_text = (self.history_dir / "runs" / "sized.jsonl").read_text(encoding="utf-8")
        self.assertIn('"work_order_lines":4', history_text)
        self.assertIn('"work_order_top_level_bullets":3', history_text)
        self.assertIn('"work_order_file_targets":2', history_text)

    def run_unowned_command(
        self, *arguments: str, at: int
    ) -> subprocess.CompletedProcess[str]:
        """Invoke the recorder the way a caller outside a launcher would."""
        environment = os.environ.copy()
        environment["PLAN_DELEGATE_HISTORY_DIR"] = str(self.history_dir)
        environment["PLAN_DELEGATE_CONFIG"] = str(self.config_file)
        environment["AGENTS_CONFIG_FILE"] = str(self.agents_config_file)
        environment["TZ"] = "UTC"
        environment["PLAN_DELEGATE_NOW_EPOCH"] = str(at)
        _ = environment.pop("PLAN_DELEGATE_PASS_OWNER", None)
        if self.team_slot:
            environment["PLAN_DELEGATE_TEAM_ROLE"] = self.team_slot
        else:
            _ = environment.pop("PLAN_DELEGATE_TEAM_ROLE", None)
        return subprocess.run(
            ["python3", str(SCRIPT), *arguments],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )

    def test_start_pass_outside_a_launcher_is_refused(self) -> None:
        session_dir = self.start_run("owned", 20_000)
        _ = self.run_command(
            "start-phase",
            "--session-dir",
            str(session_dir),
            "--phase-id",
            "3",
            "--phase-title",
            "Retry handling",
            at=20_010,
        )
        failure = self.run_unowned_command(
            "start-pass",
            "--session-dir",
            str(session_dir),
            "--pass-kind",
            "review",
            "--activity",
            "reviewing by hand",
            "--called-task",
            "delegate.review",
            "--called-family",
            "codex",
            "--called-model",
            "gpt-called",
            at=20_020,
        )
        self.assertNotEqual(failure.returncode, 0)
        self.assertIn("launcher", failure.stderr)
        history = self.history_dir / "runs" / "owned.jsonl"
        self.assertNotIn("pass_started", history.read_text(encoding="utf-8"))

    def test_finish_pass_outside_a_launcher_only_cancels_an_orphan(self) -> None:
        session_dir = self.start_run("orphan", 20_000)
        self.start_phase_and_pass(session_dir, 20_010)
        unowned = self.run_unowned_command(
            "finish-pass", "--session-dir", str(session_dir), "--status", "completed", at=20_030
        )
        self.assertNotEqual(unowned.returncode, 0)
        self.assertIn("launcher", unowned.stderr)
        wrong_status = self.run_unowned_command(
            "finish-pass",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed",
            "--orphaned-launcher",
            at=20_040,
        )
        self.assertNotEqual(wrong_status.returncode, 0)
        self.assertIn("--status canceled", wrong_status.stderr)
        canceled = self.run_unowned_command(
            "finish-pass",
            "--session-dir",
            str(session_dir),
            "--status",
            "canceled",
            "--orphaned-launcher",
            at=20_050,
        )
        self.assertEqual(canceled.returncode, 0, canceled.stderr)
        history = (self.history_dir / "runs" / "orphan.jsonl").read_text(encoding="utf-8")
        self.assertIn('"status":"canceled"', history.replace(" ", ""))
        repeated = self.run_unowned_command(
            "finish-pass",
            "--session-dir",
            str(session_dir),
            "--status",
            "canceled",
            "--orphaned-launcher",
            at=20_060,
        )
        self.assertNotEqual(repeated.returncode, 0)
        self.assertIn("No pass is open", repeated.stderr)

    def pass_events(self, name: str, event_type: str) -> list[dict[str, object]]:
        return [
            event
            for event in self.read_events(name)
            if event.get("event_type") == event_type
        ]

    def test_a_pass_records_a_digest_of_the_configuration_it_ran_under(self) -> None:
        _ = self.agents_config_file.write_text(
            "[delegate.implementation.codex]\nagent = gpt-called\n", encoding="utf-8"
        )
        session_dir = self.start_run("digest", 20_000)
        self.start_phase_and_pass(session_dir, 20_010)
        expected = hashlib.sha256(
            self.agents_config_file.read_bytes() + self.config_file.read_bytes()
        ).hexdigest()[:12]
        started = self.pass_events("digest", "pass_started")
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0].get("config_digest"), expected)

        # A conf edited between two passes has to move the digest, or grouping by
        # it would compare passes that ran under configurations that differ.
        self.write_interval(240)
        _ = self.run_command(
            "start-pass",
            "--session-dir",
            str(session_dir),
            "--pass-kind",
            "review",
            "--activity",
            "reviewing the retry path",
            "--called-task",
            "delegate.review",
            "--called-family",
            "codex",
            "--called-model",
            "gpt-called",
            "--called-effort",
            "high",
            at=20_100,
        )
        started = self.pass_events("digest", "pass_started")
        self.assertEqual(len(started), 2)
        second = cast(str, started[1]["config_digest"])
        self.assertEqual(len(second), 12)
        self.assertNotEqual(second, expected)

    def test_a_pass_records_the_team_seat_that_launched_it(self) -> None:
        """Which of the three members recorded this pass."""
        self.team_slot = "impl"
        session_dir = self.start_run("slot", 23_000)
        self.start_phase_and_pass(session_dir, 23_010)
        started = self.pass_events("slot", "pass_started")
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0].get("team_slot"), "impl")

    def test_a_pass_with_no_seat_records_an_empty_team_slot(self) -> None:
        """Empty is unknown, not a seat name.

        Passes predating the field, and any recorder outside implement.sh, have
        to stay separable from a launcher that really is the `impl` member.
        """
        session_dir = self.start_run("no-slot", 24_000)
        self.start_phase_and_pass(session_dir, 24_010)
        started = self.pass_events("no-slot", "pass_started")
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0].get("team_slot"), "")

    def test_an_unreadable_conf_leaves_the_digest_empty_and_records_the_pass(self) -> None:
        session_dir = self.start_run("no-registry", 21_000)
        self.start_phase_and_pass(session_dir, 21_010)
        started = self.pass_events("no-registry", "pass_started")
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0].get("config_digest"), "")

    def test_the_finished_pass_carries_the_seat_that_opened_it(self) -> None:
        """The reader joins on finished events, so the seat has to survive there.

        Taken from the pass record rather than the environment: the phase ends
        under the unit director, which holds no seat at all, and the pass it closes
        still has to name the member that opened it.
        """
        self.team_slot = "review"
        session_dir = self.start_run("slot-finish", 25_000)
        self.start_phase_and_pass(session_dir, 25_010)
        self.team_slot = ""
        _ = self.run_command(
            "finish-phase",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed",
            at=25_500,
        )
        finished = self.pass_events("slot-finish", "pass_finished")
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0].get("team_slot"), "review")

    def start_phase(self, session_dir: Path, at: int) -> None:
        _ = self.run_command(
            "start-phase",
            "--session-dir",
            str(session_dir),
            "--phase-id",
            "3",
            "--phase-title",
            "Retry handling",
            at=at,
        )

    def run_board(self, session_dir: Path, *arguments: str, at: int) -> None:
        """Drive board.sh on the test's clock, so its stamps and the recorder's
        events sit on one timeline instead of the board landing in the present."""
        environment = os.environ.copy()
        environment["PLAN_DELEGATE_NOW_EPOCH"] = str(at)
        _ = subprocess.run(
            ["bash", str(BOARD), arguments[0], str(session_dir), *arguments[1:]],
            check=True,
            capture_output=True,
            env=environment,
        )

    def start_slot_pass(
        self,
        session_dir: Path,
        slot: str,
        pass_kind: str,
        at: int,
        called_model: str = "gpt-called",
        fix_pass: int = 0,
    ) -> None:
        """Open a pass the way one member of a phase team opens its own."""
        self.team_slot = slot
        _ = self.run_command(
            "start-pass",
            "--session-dir",
            str(session_dir),
            "--pass-kind",
            pass_kind,
            "--fix-pass",
            str(fix_pass),
            "--activity",
            f"{slot or 'unslotted'} work",
            "--called-task",
            "delegate.implementation",
            "--called-family",
            "codex",
            "--called-model",
            called_model,
            "--called-effort",
            "high",
            at=at,
        )

    def finish_slot_pass(
        self,
        session_dir: Path,
        slot: str,
        status: str,
        at: int,
    ) -> None:
        self.team_slot = slot
        _ = self.run_command(
            "finish-pass",
            "--session-dir",
            str(session_dir),
            "--status",
            status,
            at=at,
        )

    def pass_slots(self, session_dir: Path) -> dict[str, dict[str, object]]:
        """The recorder's per-slot pass records, as they sit on disk."""
        parsed: object = json.loads(  # pyright: ignore[reportAny]
            (session_dir / "progress_history_state.json").read_text(encoding="utf-8")
        )
        state = cast(dict[str, object], parsed)
        slots = cast(dict[str, object], state["pass"])
        return {slot: cast(dict[str, object], record) for slot, record in slots.items()}

    def demote_to_legacy_pass(self, session_dir: Path, keep_seat: bool) -> None:
        """Rewrite state the way a session that started before seats holds it.

        One pass object under the key rather than a map, and the seat under the
        `team_role` name it was stamped with for a day, which is what every run
        already in flight has on disk when this recorder replaces the old one.
        """
        path = session_dir / "progress_history_state.json"
        parsed: object = json.loads(path.read_text(encoding="utf-8"))  # pyright: ignore[reportAny]
        state = cast(dict[str, object], parsed)
        slots = cast(dict[str, object], state["pass"])
        [record_value] = list(slots.values())
        record = cast(dict[str, object], record_value)
        seat = record.pop("team_slot", "")
        if keep_seat:
            record["team_role"] = seat
        state["pass"] = record
        _ = path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")

    def test_two_slots_hold_a_pass_of_their_own_at_the_same_time(self) -> None:
        """A phase team runs a launcher per slot and the recorder holds them all.

        With a single pass the second launcher to start closed the first, so the
        ledger described whichever member happened to finish last.
        """
        session_dir = self.start_run("two-slots", 40_000)
        self.start_phase(session_dir, 40_010)
        self.start_slot_pass(session_dir, "impl", "impl", 40_020)
        self.start_slot_pass(session_dir, "test", "fix", 40_030)
        slots = self.pass_slots(session_dir)
        self.assertEqual(sorted(slots), ["impl", "test"])
        self.assertEqual(slots["impl"]["status"], "active")
        self.assertEqual(slots["test"]["status"], "active")
        self.assertEqual(
            [
                event.get("team_slot")
                for event in self.pass_events("two-slots", "pass_started")
            ],
            ["impl", "test"],
        )
        self.assertEqual(self.pass_events("two-slots", "pass_finished"), [])

    def test_three_slots_each_record_a_start_and_a_finish_of_their_own(self) -> None:
        """The whole team's work reaches the ledger, in the order it happened."""
        session_dir = self.start_run("three-slots", 41_000)
        self.start_phase(session_dir, 41_010)
        for offset, (slot, kind) in enumerate(
            (("impl", "impl"), ("test", "fix"), ("review", "review"))
        ):
            self.start_slot_pass(session_dir, slot, kind, 41_020 + offset)
        for offset, slot in enumerate(("test", "review", "impl")):
            self.finish_slot_pass(session_dir, slot, "completed", 41_100 + offset)
        started = self.pass_events("three-slots", "pass_started")
        self.assertEqual(
            [event.get("team_slot") for event in started],
            ["impl", "test", "review"],
        )
        finished = self.pass_events("three-slots", "pass_finished")
        self.assertEqual(
            [event.get("team_slot") for event in finished],
            ["test", "review", "impl"],
        )
        self.assertEqual(
            {str(event.get("status")) for event in finished},
            {"completed"},
        )
        # Each finish names the window its own slot opened, never a peer's.
        self.assertEqual(
            {
                str(event.get("team_slot")): str(event.get("pass_instance_id"))
                for event in finished
            },
            {
                slot: str(record["instance_id"])
                for slot, record in self.pass_slots(session_dir).items()
            },
        )

    def test_a_slot_reopening_a_pass_interrupts_only_its_own(self) -> None:
        """A stale pass is this slot's problem and nobody else's.

        The peers belong to launchers still waiting on their own agents, so the
        second dispatch of one member must leave their windows untouched.
        """
        session_dir = self.start_run("reopen", 42_000)
        self.start_phase(session_dir, 42_010)
        self.start_slot_pass(session_dir, "impl", "impl", 42_020)
        self.start_slot_pass(session_dir, "test", "fix", 42_030)
        peer_instance = self.pass_slots(session_dir)["test"]["instance_id"]
        self.start_slot_pass(session_dir, "impl", "fix", 42_040)
        finished = self.pass_events("reopen", "pass_finished")
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0].get("team_slot"), "impl")
        self.assertEqual(finished[0].get("status"), "interrupted")
        slots = self.pass_slots(session_dir)
        self.assertEqual(slots["test"]["status"], "active")
        self.assertEqual(slots["test"]["instance_id"], peer_instance)
        self.assertEqual(slots["impl"]["status"], "active")

    def test_a_peer_cannot_finish_the_pass_another_slot_opened(self) -> None:
        """finish-pass answers for the slot the environment names and no other.

        The launcher that finishes is the launcher that started; a member that
        closed a peer's window would end a pass whose agent is still working.
        """
        session_dir = self.start_run("peer-finish", 43_000)
        self.start_phase(session_dir, 43_010)
        self.start_slot_pass(session_dir, "impl", "impl", 43_020)
        self.finish_slot_pass(session_dir, "test", "completed", 43_100)
        self.assertEqual(self.pass_events("peer-finish", "pass_finished"), [])
        self.assertEqual(self.pass_slots(session_dir)["impl"]["status"], "active")

    def test_a_legacy_single_pass_object_still_finishes(self) -> None:
        """A run already in flight keeps the pass it had open.

        Its state holds one pass object instead of a map, so it reads as the
        entry for the slot recorded on it and its launcher closes it exactly as
        it would have.
        """
        session_dir = self.start_run("legacy", 44_000)
        self.start_phase(session_dir, 44_010)
        self.start_slot_pass(session_dir, "impl", "impl", 44_020)
        instance = self.pass_slots(session_dir)["impl"]["instance_id"]
        self.demote_to_legacy_pass(session_dir, keep_seat=True)
        self.finish_slot_pass(session_dir, "impl", "completed", 44_100)
        finished = self.pass_events("legacy", "pass_finished")
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0].get("pass_instance_id"), instance)
        self.assertEqual(finished[0].get("team_slot"), "impl")
        self.assertEqual(self.pass_slots(session_dir)["impl"]["status"], "completed")

    def test_a_legacy_pass_with_no_slot_reads_as_the_empty_slot(self) -> None:
        """State written before the slot field existed carries no slot at all.

        Empty is a slot like any other, so the recorder that opened it without
        one is the recorder that closes it.
        """
        session_dir = self.start_run("legacy-unslotted", 45_000)
        self.start_phase(session_dir, 45_010)
        self.start_slot_pass(session_dir, "", "impl", 45_020)
        instance = self.pass_slots(session_dir)[""]["instance_id"]
        self.demote_to_legacy_pass(session_dir, keep_seat=False)
        self.finish_slot_pass(session_dir, "", "completed", 45_100)
        finished = self.pass_events("legacy-unslotted", "pass_finished")
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0].get("pass_instance_id"), instance)
        self.assertEqual(finished[0].get("team_slot"), "")
        self.assertEqual(self.pass_slots(session_dir)[""]["status"], "completed")

    def test_finishing_the_run_closes_every_open_slot(self) -> None:
        """A run that stops mid-phase leaves no window open behind it.

        The launchers that would have closed the peers are the processes that
        went away with the run, so the cleanup that ends the phase has to close
        all of them or they stay open in the ledger forever.
        """
        session_dir = self.start_run("run-end", 46_000)
        self.start_phase(session_dir, 46_010)
        for offset, (slot, kind) in enumerate(
            (("impl", "impl"), ("test", "fix"), ("review", "review"))
        ):
            self.start_slot_pass(session_dir, slot, kind, 46_020 + offset)
        self.team_slot = ""
        _ = self.run_command(
            "finish-run",
            "--session-dir",
            str(session_dir),
            "--status",
            "stopped",
            at=46_200,
        )
        finished = self.pass_events("run-end", "pass_finished")
        self.assertEqual(
            sorted(str(event.get("team_slot")) for event in finished),
            ["impl", "review", "test"],
        )
        self.assertEqual(
            {str(event.get("status")) for event in finished},
            {"interrupted"},
        )
        self.assertEqual(
            {str(record["status"]) for record in self.pass_slots(session_dir).values()},
            {"interrupted"},
        )

    def test_a_round_gives_every_open_seat_its_own_column(self) -> None:
        """Two agents at work read across one row, not as whichever wrote last."""
        session_dir = self.start_run("live-rows", 47_000)
        self.start_phase(session_dir, 47_010)
        self.start_slot_pass(session_dir, "impl", "impl", 47_020, called_model="gpt-impl")
        self.start_slot_pass(session_dir, "test", "test", 47_030, called_model="gpt-test")
        self.team_slot = ""
        header = self.run_progress(session_dir, 47_100)
        rows = self.table_rows(
            header,
            ["Stage", "Start", "Elapsed", "Agent 1", "Agent 2", "Result"],
        )
        # One row, because both seats are working one round. Each carries its
        # own elapsed: they launch together but do not finish together, and a
        # single shared number would hide exactly that.
        self.assertEqual(
            [(row[0], *row[3:5], row[2], row[5]) for row in rows],
            [
                (
                    "Impl",
                    "impl 1m running",
                    "test 1m running",
                    "1m",
                    "running",
                )
            ],
        )
        # The unit director holds no slot, so its report names the window that
        # opened first rather than the one that opened last.
        self.assertIn("▸ **Impl - implementing**", header)

    def test_a_seat_that_says_it_is_held_up_does_not_read_as_working(self) -> None:
        """A seat waiting on its peer has an open window and the peer's clock."""
        session_dir = self.start_run("waiting", 51_000)
        self.start_phase(session_dir, 51_010)
        for slot, kind in (("impl", "impl"), ("test", "test")):
            self.start_slot_pass(session_dir, slot, kind, 51_020)
        self.run_board(
            session_dir, "post", "test", "status",
            "Removing a redundant assertion", at=51_100,
        )
        self.run_board(
            session_dir, "post", "impl", "status",
            "Waiting for the test owner's final edit to land", at=51_110,
        )
        self.team_slot = ""
        rows = self.table_rows(
            self.run_progress(session_dir, 51_200),
            ["Stage", "Start", "Elapsed", "Agent 1", "Agent 2", "Result"],
        )
        # Nothing in the pass records separates these two: both windows are
        # open and both clocks read the same. One is sitting on the other, and
        # only what each seat said last can say so.
        self.assertEqual(
            list(rows[-1][3:5]),
            ["impl 3m waiting", "test 3m running"],
        )
        # The board has a kind for it as well as words, and both count: a seat
        # that posted `blocked` said the same thing in the field rather than in
        # the sentence.
        self.run_board(
            session_dir, "post", "test", "blocked",
            "the integration-denial regression has not landed yet", at=51_220,
        )
        rows = self.table_rows(
            self.run_progress(session_dir, 51_300),
            ["Stage", "Start", "Elapsed", "Agent 1", "Agent 2", "Result"],
        )
        self.assertEqual(
            list(rows[-1][3:5]),
            ["impl 4m waiting", "test 4m waiting"],
        )

    def test_staggered_launcher_registers_do_not_split_a_round(self) -> None:
        """Two launchers coming up seconds apart is one round, not a row per launch."""
        session_dir = self.start_run("stagger", 46_000)
        self.start_phase(session_dir, 46_010)
        self.start_slot_pass(session_dir, "impl", "impl", 46_020)
        # What implement.sh writes: each launcher registers with its opening role
        # stamped, and they land a few seconds apart because they start in
        # sequence. That stagger is a roll-call, not the team changing shape.
        for offset, slot, role in ((0, "impl", "impl"), (3, "test", "test")):
            self.run_board(session_dir, "post", slot, "register", f"role={role}; up", at=46_020 + offset)
        self.start_slot_pass(session_dir, "test", "test", 46_023)
        self.team_slot = ""
        rows = self.table_rows(
            self.run_progress(session_dir, 50_540),
            ["Stage", "Start", "Elapsed", "Agent 1", "Agent 2", "Result"],
        )
        # One row. Before the register/handoff split this rendered a near-empty
        # row per launch, seconds long, ahead of the real one.
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            (rows[0][0], *rows[0][3:5], rows[0][2]),
            (
                "Impl",
                "impl 1h15m running",
                "test 1h15m running",
                "1h15m",
            ),
        )

    def test_a_round_gains_a_row_each_time_its_seats_change_role(self) -> None:
        """Seats recruit each other, and every new shape gets its own row."""
        session_dir = self.start_run("recruit", 49_000)
        self.start_phase(session_dir, 49_010)
        for slot in ("impl", "test"):
            self.run_board(session_dir, "post", slot, "register", "up", at=49_015)
        # Opens one writing and one testing.
        for slot, role in (("impl", "impl"), ("test", "test")):
            self.run_board(session_dir, "role", slot, role, "opening", at=49_020)
        for slot, kind in (("impl", "impl"), ("test", "test")):
            self.start_slot_pass(session_dir, slot, kind, 49_020)
        self.team_slot = ""
        # The test seat is recruited across to write, then both converge on
        # review.
        self.run_board(session_dir, "role", "test", "impl", "recruited", at=49_320)
        for slot in ("impl", "test"):
            self.run_board(session_dir, "role", slot, "review", "converging", at=49_620)
        header = self.run_progress(session_dir, 49_800)
        rows = self.table_rows(
            header,
            ["Stage", "Start", "Elapsed", "Agent 1", "Agent 2", "Result"],
        )
        # One round, three shapes. The label names the round once: a repeat on
        # the continuation rows would read as three rounds rather than as one
        # team moving, and the result belongs to the round, so it sits on the
        # row that closes it.
        self.assertEqual(
            [(row[0], *row[3:5], row[2]) for row in rows],
            [
                ("Impl", "impl 5m done", "test 5m done", "5m"),
                ("", "impl 5m done", "impl 5m done", "5m"),
                ("", "review 3m running", "review 3m running", "3m"),
            ],
        )
        # A continuation row names the movement that opened it in its result
        # cell, so the reader is not left diffing two rows of cells to find
        # where the team moved; the round's tally still sits on the closing
        # row, after the movement that opened it.
        self.assertEqual(
            [row[5] for row in rows],
            [
                "",
                "Agent 2 → impl",
                "Agent 1 → review, Agent 2 → review; running",
            ],
        )
        # Which delegate is in which seat sits under the table, once, and the
        # unit director is left out of it -- the reader is the unit director. Each
        # seat carries its last board line and that line's age, because a role
        # alone -- and an open pass window, which grows either way -- cannot say
        # whether a seat is working or has been silent since it took the role.
        for label, slot in (("Agent 1", "impl"), ("Agent 2", "test")):
            self.assertIn(
                f"- **{label}** ({slot}) gpt-called high · 3m ago · handoff: converging",
                header,
            )

    def test_a_role_reannouncement_does_not_split_a_round(self) -> None:
        """A handoff that re-states the role its slot already holds is not a movement.

        What live runs write: each seat opens with a `board.sh role` seconds
        after its launcher registered the same role, and a seat routes narration
        through `role` because it is the command that takes a note. Every such
        line used to open a near-empty row whose columns matched the row above.
        """
        session_dir = self.start_run("reannounce", 52_000)
        self.start_phase(session_dir, 52_010)
        # The test slot is recruited to write from second zero, the shape
        # fe9ae569 ran: its launcher registers role=impl and its pass kind is
        # impl, so the role it holds is not the one its slot name suggests.
        for offset, slot, kind in ((0, "impl", "impl"), (3, "test", "impl")):
            self.run_board(
                session_dir, "post", slot, "register", f"role={kind}; up", at=52_020 + offset
            )
            self.start_slot_pass(session_dir, slot, kind, 52_020 + offset)
        # The opening announcements, a second apart, each naming the role the
        # register already stamped.
        for offset, slot, kind in ((15, "impl", "impl"), (16, "test", "impl")):
            self.run_board(session_dir, "role", slot, kind, "opening task", at=52_020 + offset)
        # Narration routed through `role`, minutes apart, role unchanged.
        self.run_board(session_dir, "role", "test", "impl", "rechecking storage", at=53_500)
        self.run_board(session_dir, "role", "test", "impl", "validating exports", at=54_100)
        self.team_slot = ""
        rows = self.table_rows(
            self.run_progress(session_dir, 54_400),
            ["Stage", "Start", "Elapsed", "Agent 1", "Agent 2", "Result"],
        )
        # One row: nothing moved, so nothing splits.
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            (rows[0][0], *rows[0][3:5]),
            ("Impl", "impl 39m running", "impl 39m running"),
        )

    def test_movements_seconds_apart_are_one_row(self) -> None:
        """Seats converging one `board.sh role` at a time is one movement.

        The calls land seconds apart because the seats run in sequence, and a
        boundary per call would put a near-empty row on each. One boundary, and
        the roles the later calls carry describe the row from its start.
        """
        session_dir = self.start_run("converge", 55_000)
        self.start_phase(session_dir, 55_010)
        for slot, role in (("impl", "impl"), ("test", "test")):
            self.run_board(session_dir, "post", slot, "register", f"role={role}; up", at=55_020)
            self.start_slot_pass(session_dir, slot, role, 55_020)
        for offset, slot in ((300, "impl"), (302, "test")):
            self.run_board(session_dir, "role", slot, "review", "converging", at=55_020 + offset)
        self.team_slot = ""
        rows = self.table_rows(
            self.run_progress(session_dir, 55_500),
            ["Stage", "Start", "Elapsed", "Agent 1", "Agent 2", "Result"],
        )
        self.assertEqual(
            [(row[0], *row[3:5], row[5]) for row in rows],
            [
                ("Impl", "impl 5m done", "test 5m done", ""),
                (
                    "",
                    "review 3m running",
                    "review 3m running",
                    "Agent 1 → review, Agent 2 → review; running",
                ),
            ],
        )

    def run_gate(
        self,
        session_dir: Path,
        text: str,
        started_at: int,
        finished_at: int | None,
        passed: bool = True,
    ) -> None:
        """One verification command, recorded the way the unit director runs it."""
        _ = self.run_command(
            "start-activity",
            "--session-dir",
            str(session_dir),
            "--label",
            "Verification",
            "--activity",
            text,
            at=started_at,
        )
        if finished_at is None:
            return
        _ = self.run_command(
            "finish-activity",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed" if passed else "error",
            "--result",
            "pass" if passed else "fail",
            at=finished_at,
        )

    def test_a_verification_block_is_one_row_with_its_gates_beneath(self) -> None:
        """Gates run one command at a time, and a row per command was noise.

        The pattern a live run rendered as seven anonymous rows: two gates
        fail, the unit director repairs, the reruns pass. One row tells the
        block's outcome; the notes under the table name each gate, with a
        retry folded into the gate it reran rather than listed as a fresh one.
        """
        base = 70_000
        session_dir = self.start_run("gates", base - 100)
        self.start_phase(session_dir, base - 50)
        self.run_gate(session_dir, "test hana", base, base + 30, passed=False)
        self.run_gate(session_dir, "test hana_catalyst", base + 31, base + 36)
        self.run_gate(session_dir, "test hana_catalyst identity", base + 37, base + 39)
        self.run_gate(session_dir, "lint hana", base + 40, base + 47, passed=False)
        self.run_gate(session_dir, "lint hana_catalyst", base + 48, base + 49)
        self.run_gate(session_dir, "test hana", base + 107, base + 170)
        self.run_gate(session_dir, "lint hana", base + 171, base + 182)
        self.run_gate(session_dir, "doc hana", base + 200, None)
        self.team_slot = ""
        header = self.run_progress(session_dir, base + 212)
        rows = self.table_rows(
            header,
            ["Stage", "Start", "Elapsed", "Agent 1", "Agent 2", "Result"],
        )
        self.assertEqual(
            [(row[0], row[2], *row[3:5], row[5]) for row in rows],
            [("Verification", "3m", "-", "-", "gate 6 running")],
        )
        for line in (
            "- **Verification** (unit director) · 6 gates:",
            "  - ✗→✓ test hana · 30s failed · passed on rerun 1m",
            "  - ✓ test hana_catalyst · 5s",
            "  - ✓ test hana_catalyst identity · 2s",
            "  - ✗→✓ lint hana · 7s failed · passed on rerun 11s",
            "  - ✓ lint hana_catalyst · 1s",
            "  - ▸ doc hana · running 12s",
        ):
            self.assertIn(line, header)

    def test_a_finished_verification_block_reports_its_retries(self) -> None:
        """The Result cell alone answers whether verification went clean."""
        base = 74_000
        session_dir = self.start_run("gates-done", base - 100)
        self.start_phase(session_dir, base - 50)
        self.run_gate(session_dir, "test hana", base, base + 30, passed=False)
        self.run_gate(session_dir, "lint hana", base + 40, base + 47)
        self.run_gate(session_dir, "test hana", base + 107, base + 170)
        # A pass keeps the report alive once the gates are over; the block
        # stays a closed stage beside it.
        self.start_slot_pass(session_dir, "test", "review", base + 300)
        self.team_slot = ""
        header = self.run_progress(session_dir, base + 360)
        rows = self.table_rows(
            header,
            ["Stage", "Start", "Elapsed", "Agent 1", "Agent 2", "Result"],
        )
        self.assertEqual(
            [(row[0], row[5]) for row in rows],
            [("Verification", "1 failed, reran clean"), ("Review", "running")],
        )
        self.assertIn("- **Verification** (unit director) · 2 gates:", header)

    def test_a_second_round_is_a_second_row(self) -> None:
        """A repair round is a row of its own, named by the number it carries."""
        session_dir = self.start_run("rounds", 48_000)
        self.start_phase(session_dir, 48_010)
        for slot, kind in (("impl", "impl"), ("test", "test")):
            self.start_slot_pass(session_dir, slot, kind, 48_020)
            self.finish_slot_pass(session_dir, slot, "completed", 48_200)
        self.start_slot_pass(session_dir, "impl", "fix", 48_300, fix_pass=1)
        self.team_slot = ""
        header = self.run_progress(session_dir, 48_400)
        rows = self.table_rows(
            header,
            ["Stage", "Start", "Elapsed", "Agent 1", "Agent 2", "Result"],
        )
        self.assertEqual(
            [(row[0], row[3], row[5]) for row in rows],
            [("Impl", "impl 3m done", "clean"), ("Fix 1", "fix 1m running", "running")],
        )

    def test_finish_pass_records_the_seconds_the_delegate_was_awake(self) -> None:
        session_dir = self.start_run("awake", 22_000)
        self.start_phase_and_pass(session_dir, 22_010)
        _ = self.run_command(
            "finish-pass",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed",
            "--agent-awake-seconds",
            "1800",
            at=72_020,
        )
        finished = self.pass_events("awake", "pass_finished")
        self.assertEqual(len(finished), 1)
        # The point of the field: elapsed covers the stretch the machine spent
        # suspended, and the beat count does not.
        self.assertEqual(finished[0].get("pass_elapsed_seconds"), 50_000)
        self.assertEqual(finished[0].get("agent_awake_seconds"), 1800)

    def test_a_pass_with_nothing_counted_records_no_awake_seconds(self) -> None:
        for name, reported in (("unreported", ()), ("none-counted", ("-1",))):
            with self.subTest(name):
                session_dir = self.start_run(name, 23_000)
                self.start_phase_and_pass(session_dir, 23_010)
                awake = ("--agent-awake-seconds", *reported) if reported else ()
                _ = self.run_command(
                    "finish-pass",
                    "--session-dir",
                    str(session_dir),
                    "--status",
                    "completed",
                    *awake,
                    at=23_050,
                )
                finished = self.pass_events(name, "pass_finished")
                self.assertEqual(len(finished), 1)
                self.assertNotIn("agent_awake_seconds", finished[0])

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

    def test_neighbouring_percents_in_one_stage_draw_one_steady_eta(self) -> None:
        """A few points' change in the entered percent moves the ETA a little.

        Calibrating on the exact number gave the SWING_REPORTS history's four
        neighbouring entries suggestions of 40, 20, 55 and 15, so an hour into
        the phase the ETA read 08:03, 10:33, 07:22 and 12:13 for one unchanged
        state of the work. Drawn from the stage's history around each number,
        the four land within one five-point step and half an hour of each other.
        """
        self.seed_calibration_history("swing", "implementation", SWING_REPORTS)
        started_at = 20_000
        session_dir = self.start_run("current", started_at)
        self.start_phase_and_pass(session_dir, started_at)

        ticks = [
            self.calibrate_and_report(
                session_dir, raw_percent, "implementation", at=started_at + 3_600
            )
            for raw_percent in (78, 82, 84, 86)
        ]

        self.assertEqual(
            ticks,
            [
                (30, "today 08:53"),
                (30, "today 08:53"),
                (35, "today 08:24"),
                (35, "today 08:24"),
            ],
        )

    def test_calibration_draws_on_the_stage_the_phase_has_reached(self) -> None:
        """An 84 entered during closure is nearly done; one during implementation
        is not. The named stage's history decides the suggestion, and a report at
        another stage refuses a calibration drawn for this one."""
        self.seed_calibration_history("swing", "implementation", SWING_REPORTS)
        self.seed_calibration_history("closing", "closure", ((84, 80),))
        started_at = 20_000
        session_dir = self.start_run("current", started_at)
        self.start_phase_and_pass(session_dir, started_at)

        self.assertEqual(
            self.calibrate_and_report(session_dir, 84, "closure", at=started_at + 3_600),
            (80, "today 06:48"),
        )
        self.assertEqual(
            self.calibrate_and_report(
                session_dir, 84, "implementation", at=started_at + 3_660
            ),
            (35, "today 08:27"),
        )

        _ = self.run_command(
            "calibrate",
            "--session-dir",
            str(session_dir),
            "--candidate-percent",
            "84",
            "--cap-stage",
            "implementation",
            at=started_at + 3_720,
        )
        failure = self.run_failing_command(
            "progress",
            "--session-dir",
            str(session_dir),
            "--project-raw-percent",
            "50",
            "--project-percent",
            "50",
            "--phase-raw-percent",
            "84",
            "--phase-percent",
            "35",
            "--cap-stage",
            "closure",
            "--activity",
            "running the last gates",
            at=started_at + 3_720,
        )
        self.assertNotEqual(failure.returncode, 0)
        self.assertIn("drawn for stage implementation, not closure", failure.stderr)


    def test_eta_is_omitted_at_both_endpoints(self) -> None:
        """0% offers no rate to extend, and 100% leaves nothing to extend it over."""
        started_at = 40_000
        session_dir = self.start_run("endpoints", started_at)
        self.start_phase_and_pass(session_dir, started_at)
        unstarted = self.run_command(
            "progress",
            "--session-dir",
            str(session_dir),
            "--project-raw-percent",
            "0",
            "--project-percent",
            "0",
            "--phase-raw-percent",
            "0",
            "--phase-percent",
            "0",
            "--cap-stage",
            "implementation",
            "--activity",
            "reading the work order",
            at=started_at + 100,
        )
        self.assertIn("| Project |   0 | 00:01:40 ", unstarted)
        self.assertIn("| Phase 3 |   0 | 00:01:40 ", unstarted)
        self.assertNotIn("today", unstarted)

        finished = self.run_command(
            "progress",
            "--session-dir",
            str(session_dir),
            "--project-raw-percent",
            "100",
            "--project-percent",
            "100",
            "--phase-raw-percent",
            "100",
            "--phase-percent",
            "100",
            "--cap-stage",
            "complete",
            "--activity",
            "closing the phase",
            at=started_at + 200,
        )
        self.assertIn("| Project | 100 | 00:03:20 ", finished)
        self.assertIn("| Phase 3 | 100 | 00:03:20 ", finished)
        self.assertNotIn("today", finished)


    def write_full_config(self) -> None:
        """findings.py refuses every convergence limit it cannot read."""
        _ = self.config_file.write_text(
            "\n".join(
                (
                    "PLAN_DELEGATE_PROGRESS_INTERVAL_SECONDS=180",
                    "MIN_REPAIR_BUDGET=3",
                    "REPAIR_ROUNDS_PER_FINDING=0.5",
                    "RUNAWAY_ROUNDS=5",
                    "MAX_FIX_ATTEMPTS=2",
                    "MAX_REOPENS=2",
                    "STALLED_ROUNDS=2",
                    "MAX_CONSECUTIVE_SAME_KIND_PASSES=3",
                    "MAX_REVIEW_CANCELLATIONS=1",
                    "",
                )
            ),
            encoding="utf-8",
        )

    def run_findings(self, session_dir: Path, *arguments: str, at: int) -> str:
        environment = os.environ.copy()
        environment["PLAN_DELEGATE_HISTORY_DIR"] = str(self.history_dir)
        environment["PLAN_DELEGATE_CONFIG"] = str(self.config_file)
        environment["TZ"] = "UTC"
        environment["PLAN_DELEGATE_NOW_EPOCH"] = str(at)
        result = subprocess.run(
            ["python3", str(FINDINGS), *arguments, "--session-dir", str(session_dir)],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        return result.stdout.strip()

    def run_pass(
        self,
        session_dir: Path,
        kind: str,
        fix_pass: int,
        activity: str,
        started_at: int,
        finished_at: int,
    ) -> None:
        _ = self.run_command(
            "start-pass",
            "--session-dir",
            str(session_dir),
            "--pass-kind",
            kind,
            "--fix-pass",
            str(fix_pass),
            "--activity",
            activity,
            "--called-task",
            "delegate.implementation",
            "--called-family",
            "codex",
            "--called-model",
            "gpt-called",
            "--called-effort",
            "high",
            at=started_at,
        )
        _ = self.run_command(
            "finish-pass",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed",
            at=finished_at,
        )

    def read_events(self, name: str) -> list[dict[str, object]]:
        path = self.history_dir / "runs" / f"{name}.jsonl"
        events: list[dict[str, object]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            parsed: object = json.loads(line)  # pyright: ignore[reportAny]
            events.append(cast(dict[str, object], parsed))
        return events

    def read_state(self, session_dir: Path) -> dict[str, object]:
        state_text = (session_dir / "progress_history_state.json").read_text(
            encoding="utf-8"
        )
        parsed: object = json.loads(state_text)  # pyright: ignore[reportAny]
        return cast(dict[str, object], parsed)

    def table_rows(self, rendered: str, header: list[str]) -> list[list[str]]:
        rows = [
            [cell.strip() for cell in line.removeprefix("| ").removesuffix(" |").split(" | ")]
            for line in rendered.splitlines()
            if line.startswith("| ") and set(line) - set("|-: ")
        ]
        return rows[rows.index(header) + 1 :]

    def test_a_running_round_reports_the_role_each_seat_holds_now(self) -> None:
        """One column per seat, showing the role it holds right now."""
        self.write_full_config()
        started_at = 60_000
        session_dir = self.start_run("team", started_at)
        _ = self.run_command(
            "start-phase",
            "--session-dir",
            str(session_dir),
            "--phase-id",
            "4",
            "--phase-title",
            "Adapter wiring",
            at=started_at,
        )
        # Drive the board through board.sh rather than writing board.log by
        # hand: what is under test is that the writer and the reader agree on a
        # format, which a hand-written fixture would assert nothing about.
        for slot in ("impl", "test"):
            self.run_board(session_dir, "post", slot, "register", "up", at=started_at + 1)
            self.run_board(session_dir, "role", slot, slot, "opening", at=started_at + 2)
        # The test seat gets recruited into implementation: the slot keeps its
        # identity and its column, and only the role in the cell changes.
        self.run_board(session_dir, "role", "test", "impl", "recruited", at=started_at + 3)
        self.start_slot_pass(session_dir, "impl", "impl", started_at + 10)
        self.team_slot = ""
        header = self.run_command(
            "progress",
            "--session-dir",
            str(session_dir),
            "--project-raw-percent",
            "40",
            "--project-percent",
            "40",
            "--phase-raw-percent",
            "40",
            "--phase-percent",
            "40",
            "--cap-stage",
            "implementation",
            "--activity",
            "wiring the adapter",
            at=started_at + 300,
        )
        rows = self.table_rows(
            header,
            ["Stage", "Start", "Elapsed", "Agent 1", "Agent 2", "Result"],
        )
        # The recruited test seat reads `impl` under Agent 2: the seat keeps its
        # identity and its column, and only the role in the cell moves. It has
        # registered without opening a pass yet, so it reports the role the
        # board knows and no duration, which is the part that is genuinely
        # unknown -- not a dash, which would read as idle.
        self.assertEqual(
            [rows[0][0], *rows[0][3:5]], ["Impl", "impl 4m running", "impl"]
        )

    def test_a_closed_round_keeps_the_roles_of_its_passless_seats(self) -> None:
        """A finished round still names what its test seat was doing."""
        started_at = 62_000
        session_dir = self.start_run("closed", started_at)
        self.start_phase(session_dir, started_at)
        for slot in ("impl", "test"):
            self.run_board(session_dir, "post", slot, "register", "up", at=started_at + 1)
            self.run_board(session_dir, "role", slot, slot, "opening", at=started_at + 2)
        # One seat records a pass here; the other is board-only for this round,
        # which is what the closed round has to keep reporting.
        self.start_slot_pass(session_dir, "impl", "impl", started_at + 10)
        self.finish_slot_pass(session_dir, "impl", "completed", started_at + 310)
        # A repair round opens behind it, so the closed round is read as history
        # -- which is the case the reader was seeing collapse to a solo pass.
        self.start_slot_pass(session_dir, "impl", "fix", started_at + 320, fix_pass=1)
        self.team_slot = ""
        rows = self.table_rows(
            self.run_progress(session_dir, started_at + 400),
            ["Stage", "Start", "Elapsed", "Agent 1", "Agent 2", "Result"],
        )
        # The board knows this role after the round closes exactly as it did
        # while the round ran. Blanking it once the round ends would render
        # every finished round as a solo pass and lose the shape the team
        # worked in.
        self.assertEqual(
            [rows[0][0], *rows[0][3:5]], ["Impl", "impl 5m done", "test"]
        )

    def test_each_seat_reports_its_last_board_line_and_its_age(self) -> None:
        """The note under the table says what each seat said, and how long ago."""
        started_at = 63_000
        session_dir = self.start_run("narration", started_at)
        self.start_phase(session_dir, started_at)
        # `test` is board-only: it registers and narrates but opens no pass.
        self.run_board(session_dir, "post", "test", "register", "up", at=started_at + 1)
        # `impl` opens a pass before it has posted anything, which is the case
        # the note has to name rather than leave looking like a seat at work.
        self.start_slot_pass(session_dir, "impl", "impl", started_at + 10)
        self.team_slot = ""
        self.assertIn(
            "- **Agent 1** (impl) gpt-called high · no board line yet",
            self.run_progress(session_dir, started_at + 30),
        )
        self.run_board(
            session_dir, "post", "test", "status", "writing the token race test", at=started_at + 60
        )
        self.run_board(
            session_dir, "post", "impl", "status", "rerunning the scoped test", at=started_at + 120
        )
        header = self.run_progress(session_dir, started_at + 300)
        # The age is the whole point: a seat four minutes into one activity and a
        # seat that has said nothing read differently, where two identical role
        # words in the table do not.
        self.assertIn("- **Agent 1** (impl) gpt-called high · 3m ago · rerunning the scoped test", header)
        self.assertIn("- **Agent 2** (test) · 4m ago · writing the token race test", header)

    def test_a_finished_seat_keeps_its_own_last_words_under_the_launcher_done(self) -> None:
        """The launcher's exit post names the kind; the seat's narration names the work."""
        started_at = 64_000
        session_dir = self.start_run("ownwords", started_at)
        self.start_phase(session_dir, started_at)
        for slot in ("impl", "test"):
            self.run_board(session_dir, "post", slot, "register", "role=fix; up", at=started_at + 1)
            self.start_slot_pass(session_dir, slot, "fix", started_at + 10, fix_pass=1)
        self.run_board(
            session_dir, "post", "test", "status", "hana_catalyst tests 240 passed", at=started_at + 200
        )
        # What implement.sh posts on exit: after the seat's last line, and marked
        # as the launcher's. Without the mark every finished seat read `fix
        # finished` and the narration the reader wanted sat one line up, unseen.
        self.run_board(
            session_dir,
            "post",
            "test",
            "done",
            "launcher: fix finished; summary at impl_summary_test.txt",
            at=started_at + 260,
        )
        # `impl` never narrated, so the launcher's words are all there are.
        self.run_board(
            session_dir,
            "post",
            "impl",
            "done",
            "launcher: fix finished; summary at impl_summary_impl.txt",
            at=started_at + 270,
        )
        self.team_slot = ""
        header = self.run_progress(session_dir, started_at + 300)
        self.assertIn(
            "- **Agent 2** (test) gpt-called high · 40s ago · done: hana_catalyst tests 240 passed", header
        )
        self.assertIn(
            "- **Agent 1** (impl) gpt-called high · 30s ago · done: fix finished; summary at impl_summary_impl.txt",
            header,
        )

    def broad_review(self, session_dir: Path, lenses: tuple[str, ...], started_at: int) -> None:
        """Both seats write, then the broad review seats one pass per lens.

        Each lens takes the seat review.sh gives it and posts the register line
        the launcher posts, a second apart in the order given.
        """
        seats = {"adversary": "test", "contract": "impl", "craft": "review"}
        self.start_phase(session_dir, started_at)
        for offset, slot in enumerate(("impl", "test")):
            self.start_slot_pass(session_dir, slot, slot, started_at + 10 + offset * 10)
        for offset, slot in enumerate(("impl", "test")):
            self.finish_slot_pass(session_dir, slot, "completed", started_at + 600 + offset * 10)
        for offset, lens in enumerate(lenses):
            at = started_at + 700 + offset
            self.run_board(
                session_dir, "post", seats[lens], "register",
                f"{lens} review up; mesh=none; role=review", at=at,
            )
            self.start_slot_pass(session_dir, seats[lens], "review", at, called_model=f"gpt-{lens}")
        self.team_slot = ""

    def test_a_craft_review_draws_a_third_seat_column(self) -> None:
        """The craft lens sits in `review`, and the table gains Agent 3 for it."""
        started_at = 66_000
        session_dir = self.start_run("craft-column", started_at)
        # The craft lens lands a second ahead of the other two, which must not
        # put a sliver of review in the writers' row.
        self.broad_review(session_dir, ("craft", "adversary", "contract"), started_at)
        header = self.run_progress(session_dir, started_at + 1_000)
        rows = self.table_rows(
            header,
            ["Stage", "Start", "Elapsed", "Agent 1", "Agent 2", "Agent 3", "Result"],
        )
        self.assertEqual(
            [row[3:] for row in rows],
            [
                ["impl 9m done", "test 9m done", "-", ""],
                [
                    "review 4m running",
                    "review 4m running",
                    "review 5m running",
                    "Agent 1 → review, Agent 2 → review, Agent 3 → review; running",
                ],
            ],
        )
        self.assertIn(
            "- **Agent 3** (review) gpt-craft high · 5m ago · register: craft review up",
            header,
        )

    def test_a_review_without_the_craft_lens_keeps_two_seat_columns(self) -> None:
        """No pass in `review`, no Agent 3: not in the header, not in the note.

        The review seat's board line from an earlier phase outlives that phase,
        and must not put a note line under a table with no column for it.
        """
        started_at = 67_000
        session_dir = self.start_run("two-columns", started_at)
        self.run_board(
            session_dir, "post", "review", "status", "an earlier phase's craft notes",
            at=started_at - 100,
        )
        self.broad_review(session_dir, ("adversary", "contract"), started_at)
        header = self.run_progress(session_dir, started_at + 1_000)
        rows = self.table_rows(
            header,
            ["Stage", "Start", "Elapsed", "Agent 1", "Agent 2", "Result"],
        )
        self.assertEqual(
            [row[3:] for row in rows],
            [
                ["impl 9m done", "test 9m done", ""],
                [
                    "review 4m running",
                    "review 5m running",
                    "Agent 1 → review, Agent 2 → review; running",
                ],
            ],
        )
        self.assertNotIn("Agent 3", header)

    def test_review_trial_reports_the_phase_lens_counts_and_review_minutes(self) -> None:
        """The checkpoint notice line: one phase's ux and craft findings and review time."""
        self.write_full_config()
        started_at = 68_000
        session_dir = self.start_run("trial", started_at)
        refused = self.run_failing_command(
            "review-trial", "--session-dir", str(session_dir), at=started_at + 5
        )
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("No phase recorded", refused.stderr)

        # An earlier phase, whose review time and craft finding are not this one's.
        self.start_phase(session_dir, started_at + 10)
        self.start_slot_pass(session_dir, "review", "review", started_at + 20)
        self.finish_slot_pass(session_dir, "review", "completed", started_at + 620)
        _ = self.run_findings(
            session_dir, "open", "--severity", "minor", "--title", "earlier phase",
            "--caught-by", "delegate", "--lens", "craft", at=started_at + 630,
        )
        self.team_slot = ""
        _ = self.run_command(
            "finish-phase", "--session-dir", str(session_dir), "--status", "completed",
            at=started_at + 700,
        )

        base = started_at + 1_000
        self.start_phase(session_dir, base)
        # A writer's pass is not review time.
        self.start_slot_pass(session_dir, "impl", "impl", base + 10)
        self.finish_slot_pass(session_dir, "impl", "completed", base + 3_010)
        # Every seat's review counts, whatever its status: 600 + 900 + 1200 s.
        reviews = (("impl", 600, "completed"), ("test", 900, "completed"), ("review", 1_200, "error"))
        for slot, _, _ in reviews:
            self.start_slot_pass(session_dir, slot, "review", base + 3_100)
        for slot, elapsed, status in reviews:
            self.finish_slot_pass(session_dir, slot, status, base + 3_100 + elapsed)
        self.team_slot = ""
        # The UX helper's 350 s counts; the unit director's other work does not.
        for label, opened, closed in (("UX review", 4_400, 4_750), ("Verify", 4_800, 5_300)):
            _ = self.run_command(
                "start-activity", "--session-dir", str(session_dir),
                "--label", label, "--activity", label.lower(), at=base + opened,
            )
            _ = self.run_command(
                "finish-activity", "--session-dir", str(session_dir), at=base + closed,
            )
        for lens in ("ux", "craft", "adversary,craft", "both", "contract"):
            _ = self.run_findings(
                session_dir, "open", "--severity", "minor", "--title", f"raised by {lens}",
                "--caught-by", "delegate", "--lens", lens, at=base + 5_400,
            )

        # 3050 s is 50.8 minutes, which rounds to 51 rather than truncating to 50.
        expected = (
            "review trial: ux 1 findings, code 4 findings, review-seat minutes 51, "
            + "ux check minutes 6, ux repair minutes 0"
        )
        self.assertEqual(
            self.run_command("review-trial", "--session-dir", str(session_dir), at=base + 5_500),
            expected,
        )
        _ = self.run_command(
            "finish-phase", "--session-dir", str(session_dir), "--status", "completed",
            at=base + 5_600,
        )
        self.assertEqual(
            self.run_command("review-trial", "--session-dir", str(session_dir), at=base + 5_700),
            expected,
        )

    def test_review_trial_counts_each_code_finding_once(self) -> None:
        self.write_full_config()
        started_at = 71_000
        session_dir = self.start_run("code-findings", started_at)
        self.start_phase(session_dir, started_at + 10)

        def assert_trial_counts(ux_findings: int, code_findings: int, at: int) -> None:
            self.assertEqual(
                self.run_command(
                    "review-trial", "--session-dir", str(session_dir), at=at
                ),
                f"review trial: ux {ux_findings} findings, code {code_findings} findings, "
                + "review-seat minutes 0, ux check minutes 0, ux repair minutes 0",
            )

        for index, lens in enumerate(("adversary,contract", "contract", "contract")):
            _ = self.run_findings(
                session_dir,
                "open",
                "--severity",
                "minor",
                "--title",
                f"code finding {index}",
                "--caught-by",
                "delegate",
                "--lens",
                lens,
                at=started_at + 20 + index,
            )
        _ = self.run_findings(
            session_dir,
            "open",
            "--severity",
            "minor",
            "--title",
            "unit director finding",
            "--caught-by",
            "delegate",
            at=started_at + 30,
        )
        assert_trial_counts(0, 4, started_at + 40)

        for offset, lens, ux_findings, code_findings in (
            (50, "ux", 1, 4),
            (51, "ux,craft", 2, 5),
            (52, "both", 2, 6),
        ):
            _ = self.run_findings(
                session_dir,
                "open",
                "--severity",
                "minor",
                "--title",
                f"finding from {lens}",
                "--caught-by",
                "delegate",
                "--lens",
                lens,
                at=started_at + offset,
            )
            assert_trial_counts(ux_findings, code_findings, started_at + offset + 1)

    def test_review_trial_reports_the_screenshot_check_and_its_repair_minutes(self) -> None:
        """The check's own minutes, and only the fix rounds that covered a ux finding."""
        self.write_full_config()
        base = 72_000
        session_dir = self.start_run("uxcost", base)
        self.start_phase(session_dir, base + 10)
        _ = self.run_command(
            "start-activity", "--session-dir", str(session_dir),
            "--label", "UX review", "--activity", "judging the shots", at=base + 100,
        )
        _ = self.run_command("finish-activity", "--session-dir", str(session_dir), at=base + 250)

        def repair(finding: str, lens: str, round_number: int, seats: tuple[tuple[str, int], ...], at: int) -> None:
            _ = self.run_findings(
                session_dir, "open", "--severity", "blocker", "--title", f"{lens} finding",
                "--file", "src/lib.rs", "--caught-by", "delegate", "--lens", lens, at=at,
            )
            _ = self.run_findings(session_dir, "gate", at=at + 5)
            _ = self.run_findings(session_dir, "dispatch", "--covers", finding, at=at + 10)
            for slot, _ in seats:
                self.start_slot_pass(session_dir, slot, "fix", at + 20, fix_pass=round_number)
            for slot, elapsed in seats:
                self.finish_slot_pass(session_dir, slot, "completed", at + 20 + elapsed)
            self.team_slot = ""
            _ = self.run_findings(session_dir, "landed", at=at + 2_000)
            _ = self.run_findings(
                session_dir, "verdict", "--id", finding, "--state", "accepted",
                "--evidence", "repaired", at=at + 2_010,
            )

        # Round 1 repairs a code finding: none of its 600 s is the check's.
        repair("F001", "adversary", 1, (("impl", 600),), base + 300)
        # Round 2 repairs a ux finding on two seats: 900 + 300 s is 20 minutes.
        repair("F002", "ux", 2, (("impl", 900), ("test", 300)), base + 3_000)

        # 150 s rounds to 3 minutes, not 2.
        self.assertEqual(
            self.run_command("review-trial", "--session-dir", str(session_dir), at=base + 6_000),
            "review trial: ux 1 findings, code 1 findings, review-seat minutes 3, "
            + "ux check minutes 3, ux repair minutes 20",
        )

    def test_the_board_refuses_a_handoff_that_names_no_role(self) -> None:
        """A handoff is a role change; prose there is a movement the table never shows."""
        session_dir = self.start_run("barehandoff", 65_000)
        environment = os.environ.copy()
        environment["PLAN_DELEGATE_NOW_EPOCH"] = "65001"
        refused = subprocess.run(
            ["bash", str(BOARD), "post", str(session_dir), "test", "handoff", "round 8 done"],
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("board.sh role", refused.stderr)
        log = session_dir / "board.log"
        self.assertNotIn("handoff", log.read_text(encoding="utf-8") if log.exists() else "")
        # The verb writes the field the table reads, and is the way through.
        self.run_board(session_dir, "role", "test", "review", "converging", at=65_002)
        self.assertIn("[test] handoff: role=review converging", log.read_text(encoding="utf-8"))

    def test_a_round_falls_back_when_no_board_exists(self) -> None:
        """A phase whose team has not registered still renders a row."""
        self.write_full_config()
        started_at = 60_000
        session_dir = self.start_run("noboard", started_at)
        _ = self.run_command(
            "start-phase",
            "--session-dir",
            str(session_dir),
            "--phase-id",
            "2",
            "--phase-title",
            "Nothing posted",
            at=started_at,
        )
        _ = self.run_command(
            "start-pass",
            "--session-dir",
            str(session_dir),
            "--pass-kind",
            "impl",
            "--fix-pass",
            "0",
            "--activity",
            "working",
            "--called-task",
            "delegate.implementation",
            "--called-family",
            "codex",
            "--called-model",
            "gpt-called",
            "--called-effort",
            "high",
            at=started_at + 10,
        )
        header = self.run_command(
            "progress",
            "--session-dir",
            str(session_dir),
            "--project-raw-percent",
            "10",
            "--project-percent",
            "10",
            "--phase-raw-percent",
            "10",
            "--phase-percent",
            "10",
            "--cap-stage",
            "implementation",
            "--activity",
            "working",
            at=started_at + 200,
        )
        rows = self.table_rows(
            header,
            ["Stage", "Start", "Elapsed", "Agent 1", "Agent 2", "Result"],
        )
        # No board and no seat on the pass: the window sits in no round, so it
        # renders on its own the way every solo run always has, drawn in the
        # seat its kind names.
        self.assertEqual(
            [rows[0][0], *rows[0][3:5]], ["Impl", "impl 3m running", "-"]
        )

    def test_a_run_of_verifications_counts_as_one_stage(self) -> None:
        """Consecutive windows sharing a label spend one of the table's slots."""
        self.write_full_config()
        started_at = 61_000
        session_dir = self.start_run("verifications", started_at)
        _ = self.run_command(
            "start-phase",
            "--session-dir",
            str(session_dir),
            "--phase-id",
            "9",
            "--phase-title",
            "Retry budget",
            at=started_at,
        )
        self.run_pass(
            session_dir,
            "impl",
            0,
            "writing the retry budget",
            started_at + 10,
            started_at + 200,
        )
        # A repair round triggers verification more than once -- a failure, a
        # rerun, the scoped test that follows it -- and they land within a
        # minute of each other.
        for index in range(3):
            _ = self.run_command(
                "start-activity",
                "--session-dir",
                str(session_dir),
                "--label",
                "Verification",
                "--activity",
                "test hana_clerestory",
                at=started_at + 220 + index * 40,
            )
            _ = self.run_command(
                "finish-activity",
                "--session-dir",
                str(session_dir),
                "--status",
                "completed",
                "--result",
                "pass",
                at=started_at + 240 + index * 40,
            )
        _ = self.run_command(
            "start-pass",
            "--session-dir",
            str(session_dir),
            "--pass-kind",
            "fix",
            "--fix-pass",
            "1",
            "--activity",
            "repairing the budget",
            "--called-task",
            "delegate.implementation",
            "--called-family",
            "codex",
            "--called-model",
            "gpt-called",
            "--called-effort",
            "high",
            at=started_at + 400,
        )
        header = self.run_command(
            "progress",
            "--session-dir",
            str(session_dir),
            "--project-raw-percent",
            "40",
            "--project-percent",
            "40",
            "--phase-raw-percent",
            "60",
            "--phase-percent",
            "60",
            "--cap-stage",
            "implementation",
            "--activity",
            "repairing the budget",
            at=started_at + 440,
        )
        rows = self.table_rows(
            header,
            ["Stage", "Start", "Elapsed", "Agent 1", "Agent 2", "Result"],
        )
        # Three stages, three rows: the verification run is one stage of the
        # cap AND one row of the table, its gate-by-gate story in the notes
        # beneath. Counting its windows singly once dropped the implementation
        # the phase opened with; rendering them singly filled the table with
        # rows a reader could tell apart only by result.
        self.assertEqual(
            [row[0] for row in rows],
            ["Impl", "Verification", "Fix 1"],
        )
        self.assertNotIn("Earlier:", header)

    def test_the_stage_table_names_every_window_the_phase_opened(self) -> None:
        """Each pass and activity in start order, with what the ledger recorded."""
        self.write_full_config()
        started_at = 60_000
        session_dir = self.start_run("stages", started_at)
        _ = self.run_command(
            "start-phase",
            "--session-dir",
            str(session_dir),
            "--phase-id",
            "7",
            "--phase-title",
            "Drift records",
            at=started_at,
        )
        self.run_pass(
            session_dir,
            "impl",
            0,
            "writing the drift records",
            started_at + 10,
            started_at + 200,
        )
        self.run_pass(
            session_dir,
            "review",
            0,
            "independent review of the finished code",
            started_at + 220,
            started_at + 300,
        )
        for index in (1, 2):
            _ = self.run_findings(
                session_dir,
                "open",
                "--severity",
                "blocker",
                "--title",
                f"finding {index}",
                "--file",
                "src/lib.rs",
                "--line",
                str(index),
                "--caught-by",
                "both",
                at=started_at + 320,
            )
        _ = self.run_findings(session_dir, "gate", at=started_at + 325)
        _ = self.run_findings(session_dir, "dispatch", "--covers", "F001,F002", at=started_at + 330)
        self.run_pass(
            session_dir,
            "fix",
            1,
            "repairing both findings",
            started_at + 340,
            started_at + 500,
        )
        _ = self.run_findings(session_dir, "landed", at=started_at + 500)
        self.run_pass(
            session_dir,
            "review",
            0,
            "closure review of the repair",
            started_at + 520,
            started_at + 560,
        )
        for finding in ("F001", "F002"):
            _ = self.run_findings(
                session_dir,
                "verdict",
                "--id",
                finding,
                "--state",
                "accepted",
                "--evidence",
                "repaired",
                at=started_at + 580,
            )
        _ = self.run_command(
            "start-activity",
            "--session-dir",
            str(session_dir),
            "--label",
            "Verification",
            "--activity",
            "test hana_clerestory",
            at=started_at + 600,
        )
        _ = self.run_command(
            "finish-activity",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed",
            "--result",
            "pass",
            at=started_at + 640,
        )
        _ = self.run_command(
            "start-pass",
            "--session-dir",
            str(session_dir),
            "--pass-kind",
            "review",
            "--activity",
            "checking the remaining plan against what shipped",
            "--called-task",
            "delegate.review",
            "--called-family",
            "codex",
            "--called-model",
            "gpt-called",
            "--called-effort",
            "max",
            at=started_at + 660,
        )
        header = self.run_command(
            "progress",
            "--session-dir",
            str(session_dir),
            "--project-raw-percent",
            "50",
            "--project-percent",
            "50",
            "--phase-raw-percent",
            "95",
            "--phase-percent",
            "95",
            "--cap-stage",
            "closure",
            "--activity",
            "checking the remaining plan against what shipped",
            at=started_at + 700,
        )
        rows = self.table_rows(
            header,
            ["Stage", "Start", "Elapsed", "Agent 1", "Agent 2", "Result"],
        )
        # A solo run records no seat, so every window keeps a row and the
        # per-window finding attribution it always had -- for the last three
        # stages, which is all the table draws. The three before them are named
        # by the line above it rather than dropped without a word.
        self.assertEqual(
            [(row[0], row[2], row[5]) for row in rows],
            [
                ("Review Fix 1", "40s", "2 fixed"),
                ("Verification", "40s", "pass"),
                ("Review 3", "40s", "running"),
            ],
        )
        self.assertIn("*Earlier: 3 stages not shown - Impl through Fix 1.*", header)
        # A slotless pass is drawn in the seat its kind names -- the closure
        # review under Agent 2, the seat that reviews, where a reader looks for
        # it -- and a unit-director activity names no seat and keeps both dashes.
        self.assertEqual(
            [tuple(row[3:5]) for row in rows],
            [
                ("-", "review 40s done"),
                ("-", "-"),
                ("-", "review 40s running"),
            ],
        )
        self.assertIn("▸ **Review 3 - checking the remaining plan against what shipped**", header)
        # Which agent ran each window is the `timeline` view's question, and the
        # unit director ran verification itself, so that row has no delegate there.
        stage_rows = self.table_rows(
            self.run_command(
                "timeline",
                "--session-dir",
                str(session_dir),
                at=started_at + 700,
            ),
            ["Stage", "Main", "Delegate", "Start", "Elapsed", "Result"],
        )
        self.assertEqual((stage_rows[4][1], stage_rows[4][2]), ("gpt-main xhigh", ""))
        self.assertEqual(stage_rows[0][1], "gpt-main xhigh")

    def write_agents_registry(self) -> None:
        """A registry under the temporary home, so arm-review resolves the same
        agent review.sh would without reading this machine's real assignments."""
        _ = self.agents_config_file.write_text(
            "\n".join(
                (
                    "[assignments]",
                    "delegate=codex",
                    "",
                    "[delegate.codex]",
                    "implementation=gpt-called:xhigh",
                    "review=gpt-blind:max",
                    "",
                    "[codex.agents]",
                    "gpt-called=low,medium,high,xhigh,max",
                    "gpt-blind=low,medium,high,xhigh,max",
                    "",
                )
            ),
            encoding="utf-8",
        )

    def arm_early_review(self, session_dir: Path, at: int) -> str:
        return self.run_command(
            "arm-review",
            "--session-dir",
            str(session_dir),
            "--activity",
            "reviewing the diff the writer is still producing",
            "--called-task",
            "delegate.review",
            at=at,
        )

    def test_an_early_review_runs_as_its_own_row_beside_the_open_pass(self) -> None:
        """The one moment two agents work at once, shown as two running rows."""
        self.write_agents_registry()
        started_at = 90_000
        session_dir = self.start_run("early", started_at)
        self.start_phase_and_pass(session_dir, started_at)
        _ = self.arm_early_review(session_dir, started_at + 100)
        header = self.run_command(
            "progress",
            "--session-dir",
            str(session_dir),
            "--project-raw-percent",
            "40",
            "--project-percent",
            "40",
            "--phase-raw-percent",
            "75",
            "--phase-percent",
            "75",
            "--cap-stage",
            "implementation",
            "--activity",
            "correcting retry recovery",
            at=started_at + 160,
        )
        rows = self.table_rows(
            header,
            ["Stage", "Start", "Elapsed", "Agent 1", "Agent 2", "Result"],
        )
        # Neither window has opened a round -- the writer predates seats and the
        # armed reviewer has no pass yet -- so there is no round for the
        # reviewer to join and it keeps the row beside the writer that it
        # always had.
        self.assertEqual(
            [(row[0], row[2], row[5]) for row in rows],
            [("Fix 2", "2m", "running"), ("Review Fix 2", "1m", "running (early)")],
        )
        # The armed reviewer claims the test seat, where the adversary lens sits
        # and a lone closure reviewer reads, so it is drawn under Agent 2.
        self.assertEqual(
            [tuple(row[3:5]) for row in rows],
            [("fix 2m running", "-"), ("-", "review 1m running")],
        )
        # The report is about the writer, so the sentence beneath the table names
        # it -- not the reviewer, whose row is now the last one.
        self.assertIn("▸ **Fix 2 - correcting retry recovery**", header)

    def test_the_early_reviewers_real_pass_supersedes_its_armed_row(self) -> None:
        self.write_agents_registry()
        started_at = 91_000
        session_dir = self.start_run("adopted", started_at)
        self.start_phase_and_pass(session_dir, started_at)
        _ = self.arm_early_review(session_dir, started_at + 100)
        _ = self.run_command(
            "finish-pass",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed",
            at=started_at + 200,
        )
        _ = self.run_command(
            "start-pass",
            "--session-dir",
            str(session_dir),
            "--pass-kind",
            "review",
            "--activity",
            "reviewing the finished diff",
            "--called-task",
            "delegate.review",
            "--called-family",
            "codex",
            "--called-model",
            "gpt-blind",
            "--called-effort",
            "max",
            at=started_at + 210,
        )
        rendered = self.run_command(
            "timeline",
            "--session-dir",
            str(session_dir),
            at=started_at + 240,
        )
        stage_rows = self.table_rows(
            rendered,
            ["Stage", "Main", "Delegate", "Start", "Elapsed", "Result"],
        )
        self.assertEqual(
            [(row[0], row[5]) for row in stage_rows],
            [("Fix 2", "done"), ("Review Fix 2", "running")],
        )
        disarmed = [
            event
            for event in self.read_events("adopted")
            if event.get("event_type") == "early_review_disarmed"
        ]
        self.assertEqual([event["reason"] for event in disarmed], ["adopted"])

    def test_a_killed_early_reviewer_leaves_no_row_behind(self) -> None:
        """A marker outliving its launcher would show a phantom agent working."""
        self.write_agents_registry()
        started_at = 92_000
        session_dir = self.start_run("orphan", started_at)
        self.start_phase_and_pass(session_dir, started_at)
        _ = self.arm_early_review(session_dir, started_at + 100)
        dead = subprocess.Popen(["true"])
        _ = dead.wait()
        pid_file = session_dir / "review_pid"
        _ = pid_file.write_text(f"{dead.pid}\n", encoding="utf-8")
        os.utime(pid_file, (started_at + 110, started_at + 110))
        rendered = self.run_command(
            "timeline",
            "--session-dir",
            str(session_dir),
            at=started_at + 160,
        )
        stage_rows = self.table_rows(
            rendered,
            ["Stage", "Main", "Delegate", "Start", "Elapsed", "Result"],
        )
        self.assertEqual([row[0] for row in stage_rows], ["Fix 2"])

    def test_a_reviewer_that_failed_before_delivery_leaves_no_row_behind(self) -> None:
        """review.sh's own status retires the row a killed launcher cannot."""
        self.write_agents_registry()
        started_at = 94_000
        session_dir = self.start_run("failed-early", started_at)
        self.start_phase_and_pass(session_dir, started_at)
        status = session_dir / "review_status"
        # The status a previous pass left behind must not retire the new row.
        _ = status.write_text("reviewed\n", encoding="utf-8")
        os.utime(status, (started_at + 50, started_at + 50))
        _ = self.arm_early_review(session_dir, started_at + 100)
        armed = self.run_command(
            "timeline",
            "--session-dir",
            str(session_dir),
            at=started_at + 120,
        )
        self.assertIn("running (early)", armed)

        _ = status.write_text("error\n", encoding="utf-8")
        os.utime(status, (started_at + 130, started_at + 130))
        retired = self.run_command(
            "timeline",
            "--session-dir",
            str(session_dir),
            at=started_at + 140,
        )
        self.assertNotIn("running (early)", retired)

    def test_arming_an_early_review_without_an_open_pass_is_refused(self) -> None:
        started_at = 93_000
        session_dir = self.start_run("unpaired", started_at)
        self.start_phase_and_pass(session_dir, started_at)
        _ = self.run_command(
            "finish-pass",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed",
            at=started_at + 60,
        )
        failure = self.run_failing_command(
            "arm-review",
            "--session-dir",
            str(session_dir),
            "--activity",
            "reviewing nothing in particular",
            at=started_at + 70,
        )
        self.assertNotEqual(failure.returncode, 0)
        self.assertIn("a pass must be open", failure.stderr)

    def test_an_activity_records_its_own_identity_not_the_finished_pass(self) -> None:
        """A finished pass stays in state; it used to stamp every later event."""
        started_at = 70_000
        session_dir = self.start_run("identity", started_at)
        self.start_phase_and_pass(session_dir, started_at)
        _ = self.run_command(
            "finish-pass",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed",
            at=started_at + 60,
        )
        _ = self.run_command(
            "start-activity",
            "--session-dir",
            str(session_dir),
            "--label",
            "Style",
            "--activity",
            "style-only review of the phase diff",
            at=started_at + 70,
        )
        started = [
            event
            for event in self.read_events("identity")
            if event.get("event_type") == "activity_started"
        ]
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0]["activity_label"], "Style")
        self.assertEqual(started[0]["activity_text"], "style-only review of the phase diff")
        self.assertNotIn("pass_kind", started[0])
        self.assertNotIn("called_agent", started[0])

    def test_the_timeline_reports_a_phase_the_run_has_already_finished(self) -> None:
        started_at = 80_000
        session_dir = self.start_run("recorded", started_at)
        self.start_phase_and_pass(session_dir, started_at)
        _ = self.run_command(
            "finish-pass",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed",
            at=started_at + 100,
        )
        _ = self.run_command(
            "finish-phase",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed",
            at=started_at + 120,
        )
        rendered = self.run_command(
            "timeline",
            "--session-dir",
            str(session_dir),
            at=started_at + 900,
        )
        self.assertIn("**Phase 3: Retry handling - elapsed 00:02:00 - completed**", rendered)
        self.assertIn(
            "| Fix 2 | gpt-main xhigh | gpt-called high | 22:13:30 | 00:01:30 | done",
            rendered,
        )

        missing = self.run_failing_command(
            "timeline",
            "--session-dir",
            str(session_dir),
            "--phase",
            "9",
            at=started_at + 900,
        )
        self.assertEqual(missing.returncode, 1)
        self.assertIn("recorded no phase 9", missing.stderr)

    def test_a_window_picks_up_the_main_agent_changing_mid_run(self) -> None:
        """`start-run` detects the unit director once, and it can change after."""
        started_at = 90_000
        session_dir = self.start_run("switched", started_at)
        transcript = self.root / ".claude" / "projects" / "demo" / "session-9.jsonl"
        transcript.parent.mkdir(parents=True)
        _ = transcript.write_text(
            "\n".join(
                (
                    json.dumps(
                        {"type": "assistant", "message": {"model": "opus-5"}, "effort": "high"}
                    ),
                    json.dumps(
                        {"type": "assistant", "message": {"model": "sonnet-5"}, "effort": "low"}
                    ),
                    "",
                )
            ),
            encoding="utf-8",
        )
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
            "impl",
            "--activity",
            "writing the retry path",
            "--called-task",
            "delegate.implementation",
            "--called-family",
            "codex",
            "--called-model",
            "gpt-called",
            "--called-effort",
            "high",
            at=started_at + 10,
            claude_session="session-9",
        )
        rendered = self.run_command(
            "timeline",
            "--session-dir",
            str(session_dir),
            at=started_at + 100,
        )
        self.assertIn("| sonnet-5 low |", rendered)
        self.assertNotIn("gpt-main xhigh", rendered)

    def test_the_eta_names_the_day_the_work_is_expected_to_land(self) -> None:
        """A duration has to be added to the clock by hand; an arrival does not."""
        session_dir = self.start_run("arrival", 0)
        _ = self.run_command(
            "start-phase",
            "--session-dir",
            str(session_dir),
            "--phase-id",
            "3",
            "--phase-title",
            "Retry handling",
            at=70_000,
        )
        _ = self.run_command(
            "start-pass",
            "--session-dir",
            str(session_dir),
            "--pass-kind",
            "impl",
            "--activity",
            "writing the retry path",
            "--called-task",
            "delegate.implementation",
            "--called-family",
            "codex",
            "--called-model",
            "gpt-called",
            "--called-effort",
            "high",
            at=70_010,
        )
        header = self.run_command(
            "progress",
            "--session-dir",
            str(session_dir),
            "--project-raw-percent",
            "10",
            "--project-percent",
            "10",
            "--phase-raw-percent",
            "50",
            "--phase-percent",
            "50",
            "--cap-stage",
            "implementation",
            "--activity",
            "writing the retry path",
            at=80_000,
        )
        self.assertIn("| Project |  10 | 22:13:20 | 1970-01-10 06:13 |", header)
        self.assertIn("| Phase 3 |  50 | 02:46:40 |   tomorrow 01:00 |", header)


    def test_the_eta_band_brackets_the_arrival_and_names_its_own_swing(self) -> None:
        """One arrival hides how firm it is; two bracket it and show the spread.

        Ten points of doubt either way, run back through the same projection the
        ETA uses. The band is asymmetric because the projection is: at 10% those
        ten points buy four days of optimism and cost nine of pessimism, and the
        parenthesised distances say so without any clock arithmetic.
        """
        session_dir = self.start_run("band", 0)
        self.start_phase_and_pass(session_dir, 70_000)
        header = self.run_command(
            "progress",
            "--session-dir",
            str(session_dir),
            "--project-raw-percent",
            "10",
            "--project-percent",
            "10",
            "--phase-raw-percent",
            "50",
            "--phase-percent",
            "50",
            "--cap-stage",
            "implementation",
            "--activity",
            "writing the retry path",
            at=80_000,
        )
        self.assertIn(
            "| 1970-01-05 15:06 (-111:06) | 1970-01-19 12:26 (+222:13) |",
            header,
        )
        self.assertIn(
            "|    tomorrow 00:04 (-00:55) |    tomorrow 02:23 (+01:23) |",
            header,
        )


    def test_the_header_names_no_position_when_ids_and_order_disagree(self) -> None:
        """A deferred earlier phase and a retired id leave three numbers apart.

        Phase 3 is in flight, one of four phases is done, and the active
        heading is the second. The header names where the run is and no
        position; the Phase row names the id and the Phases column the count.
        """
        session_dir = self.start_run("position", 0)
        plan_path = self.working_dir / "docs" / "position.md"
        _ = plan_path.write_text(
            plan_path.read_text(encoding="utf-8")
            + "### Phase 1 — Deferred  · status: todo\n\n"
            + "### Phase 3 — Live work  · status: todo\n\n"
            + "### Phase 4 — Shrunk archive form (`bbac234`)\n\n"
            + "### Phase 5 — Waiting  · status: todo\n\n",
            encoding="utf-8",
        )
        self.start_phase_and_pass(session_dir, 70_000)
        header = self.run_command(
            "progress",
            "--session-dir",
            str(session_dir),
            "--project-raw-percent",
            "90",
            "--project-percent",
            "90",
            "--phase-raw-percent",
            "50",
            "--phase-percent",
            "50",
            "--cap-stage",
            "implementation",
            "--activity",
            "writing the retry path",
            at=80_000,
        )
        self.assertEqual(
            header.splitlines()[0],
            "**bevy_hana_rubric - feature/rubric**",
        )
        # The supplied 90 is advisory; one finished phase and a half-done
        # second of four is 38.
        self.assertIn("| Project |  38 | ", header)
        self.assertIn("| 1 of 4 done |", header)
        self.assertIn("| Phase 3 |", header)

    def test_the_header_names_a_worktree_once_when_its_branch_shares_the_name(
        self,
    ) -> None:
        _ = subprocess.run(
            ["git", "symbolic-ref", "HEAD", "refs/heads/bevy_hana_rubric"],
            cwd=self.working_dir,
            check=True,
            capture_output=True,
            text=True,
        )
        session_dir = self.start_run("same_name", 0)
        self.start_phase_and_pass(session_dir, 70_000)
        header = self.run_command(
            "progress",
            "--session-dir",
            str(session_dir),
            "--project-raw-percent",
            "50",
            "--project-percent",
            "50",
            "--phase-raw-percent",
            "50",
            "--phase-percent",
            "50",
            "--cap-stage",
            "implementation",
            "--activity",
            "writing the retry path",
            at=80_000,
        )
        self.assertEqual(header.splitlines()[0], "**bevy_hana_rubric**")


class PhaseCountTests(unittest.TestCase):
    """A plan's phase headings take three forms; all three must be counted.

    Counting only the `· status:` forms silently drops every shrunk phase,
    which under-reported a 68%-complete plan as 36%.
    """

    temporary: tempfile.TemporaryDirectory[str]  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)

    def _count(self, body: str, phase_percent: int = 0) -> dict[str, object]:
        plan = Path(self.temporary.name) / "plan.md"
        _ = plan.write_text(body, encoding="utf-8")
        completed = subprocess.run(
            [
                "python3",
                str(SCRIPT),
                "phase-count",
                "--plan-doc",
                str(plan),
                "--phase-percent",
                str(phase_percent),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return cast("dict[str, object]", json.loads(completed.stdout))

    def test_counts_all_three_heading_forms(self) -> None:
        counts = self._count(
            "### Phase 1 — Shrunk archive form (`bbac234`)\n"
            + "\n"
            + "### Phase 2 — Another shrunk one (`8c5053a`)\n"
            + "\n"
            + "### Phase 3 — Completed, not yet shrunk  · status: done\n"
            + "\n"
            + "### Phase 4 — Live work  · status: todo\n"
        )
        self.assertEqual(counts["done"], 3)
        self.assertEqual(counts["todo"], 1)
        self.assertEqual(counts["total"], 4)

    def test_review_section_heading_is_not_a_phase(self) -> None:
        counts = self._count(
            "### Phase 1 — Real phase  · status: done\n"
            + "\n"
            + "### Phase 1 Review\n"
            + "\n"
            + "### Phase 2 — Real phase  · status: todo\n"
        )
        self.assertEqual(counts["total"], 2)
        self.assertEqual(counts["done"], 1)
        self.assertEqual(counts["todo"], 1)

    def test_repeated_identifier_counts_once(self) -> None:
        counts = self._count(
            "### Phase 7 — First mention  · status: done\n"
            + "\n"
            + "#### Phase 7 — Same id again  · status: done\n"
        )
        self.assertEqual(counts["total"], 1)
        self.assertEqual(counts["duplicate_ids"], ["7"])

    def test_letter_suffixed_identifier_is_counted(self) -> None:
        counts = self._count(
            "### Phase 4 — Plain  · status: done\n"
            + "\n"
            + "### Phase 4b — Suffixed legacy id  · status: todo\n"
        )
        self.assertEqual(counts["total"], 2)
        self.assertEqual(counts["todo"], 1)

    def test_project_percent_credits_the_phase_in_flight(self) -> None:
        body = (
            "".join(
                f"### Phase {index} — Done  · status: done\n\n" for index in range(1, 4)
            )
            + "### Phase 4 — Live  · status: todo\n"
        )
        self.assertEqual(self._count(body, 0)["project_percent"], 75)
        self.assertEqual(self._count(body, 100)["project_percent"], 100)
        self.assertEqual(self._count(body, 50)["project_percent"], 88)

    def test_plan_without_phase_headings_is_unavailable(self) -> None:
        counts = self._count("# A document with no phases\n")
        self.assertIs(counts["available"], False)
        self.assertIsNone(counts["project_percent"])


class PublicPhaseHelperTests(unittest.TestCase):
    temporary: tempfile.TemporaryDirectory[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_recorder_helpers_use_public_names_without_private_aliases(self) -> None:
        with patch.dict(os.environ, {"PLAN_DELEGATE_NOW_EPOCH": "123.5"}):
            self.assertEqual(progress_history.now_epoch(), 123.5)
        self.assertEqual(
            progress_history.resolve_plan_path(self.root, "docs/plan.md"),
            (self.root / "docs" / "plan.md").resolve(),
        )
        self.assertEqual(progress_history.percent_spread(None), 10.0)
        self.assertFalse(hasattr(progress_history, "_now_epoch"))
        self.assertFalse(hasattr(progress_history, "_plan_path"))
        self.assertFalse(hasattr(progress_history, "_percent_spread"))

    def test_plan_phases_classifies_and_cleans_every_heading_form(self) -> None:
        plan = self.root / "plan.md"
        _ = plan.write_text(
            "\n".join(
                (
                    "### Phase 1 — Archived delivery (`abc1234`)",
                    "",
                    "### Phase 2 — Finished delivery  · status: done",
                    "",
                    "#### Phase 2 — Duplicate is ignored  · status: done",
                    "",
                    "### Phase 2 Review",
                    "",
                    "### Phase 3 — Live delivery  · status: todo",
                    "",
                )
            ),
            encoding="utf-8",
        )

        self.assertEqual(
            progress_history.plan_phases(plan),
            [
                {"id": "1", "title": "Archived delivery", "done": True},
                {"id": "2", "title": "Finished delivery", "done": True},
                {"id": "3", "title": "Live delivery", "done": False},
            ],
        )

    def test_plan_phases_preserves_descriptive_parentheticals(self) -> None:
        plan = self.root / "plan.md"
        _ = plan.write_text(
            "\n".join(
                (
                    "### Phase 1 — Gradients (COLRv1 solid + linear/radial)  · status: done",
                    "### Phase 2 — Support (macOS)  · status: todo",
                    "",
                )
            ),
            encoding="utf-8",
        )

        self.assertEqual(
            [phase["title"] for phase in progress_history.plan_phases(plan)],
            ["Gradients (COLRv1 solid + linear/radial)", "Support (macOS)"],
        )

    def test_plan_phases_strips_only_commit_annotations(self) -> None:
        plan = self.root / "plan.md"
        _ = plan.write_text(
            "\n".join(
                (
                    "### Phase 1 — Title (abc1234)",
                    "### Phase 2 — Title (commit abc1234def)",
                    "",
                )
            ),
            encoding="utf-8",
        )

        self.assertEqual(
            [phase["title"] for phase in progress_history.plan_phases(plan)],
            ["Title", "Title"],
        )

    def test_eta_band_seconds_exposes_the_existing_projection_math(self) -> None:
        self.assertEqual(
            progress_history.eta_band_seconds(50, 100, 10.0),
            (100, 66, 150),
        )
        self.assertIsNone(progress_history.eta_band_seconds(0, 100, 10.0))

    def test_plan_runs_finds_matching_history_oldest_first(self) -> None:
        working_dir = self.root / "worktree"
        plan = working_dir / "docs" / "plan.md"
        plan.parent.mkdir(parents=True)
        _ = plan.write_text("# Plan\n", encoding="utf-8")
        runs_dir = self.root / "history" / "runs"
        runs_dir.mkdir(parents=True)

        def write_run(name: str, plan_doc: str, started_at: int) -> Path:
            path = runs_dir / f"{name}.jsonl"
            event = {
                "event_type": "run_started",
                "working_dir": str(working_dir),
                "plan_doc": plan_doc,
                "run_started_at": started_at,
            }
            _ = path.write_text(json.dumps(event) + "\n", encoding="utf-8")
            return path

        newer = write_run("alphabetically-first", str(plan), 200)
        older = write_run("alphabetically-last", "docs/plan.md", 100)
        _ = write_run("another-plan", "docs/other.md", 50)

        with patch.dict(
            os.environ,
            {"PLAN_DELEGATE_HISTORY_DIR": str(self.root / "history")},
        ):
            self.assertEqual(progress_history.plan_runs(plan), [older, newer])

    def test_plan_runs_ignores_project_plan_inherited_without_a_run_plan(self) -> None:
        working_dir = self.root / "worktree"
        plan = working_dir / "docs" / "plan.md"
        plan.parent.mkdir(parents=True)
        _ = plan.write_text("# Plan\n", encoding="utf-8")
        runs_dir = self.root / "history" / "runs"
        runs_dir.mkdir(parents=True)
        inherited = {
            "event_type": "run_started",
            "working_dir": str(working_dir),
            "plan_doc": "",
            "project_plan_doc": str(plan),
            "run_started_at": 100,
        }
        _ = (runs_dir / "inherited.jsonl").write_text(
            json.dumps(inherited) + "\n",
            encoding="utf-8",
        )

        with patch.dict(
            os.environ,
            {"PLAN_DELEGATE_HISTORY_DIR": str(self.root / "history")},
        ):
            self.assertEqual(progress_history.plan_runs(plan), [])


if __name__ == "__main__":
    _ = unittest.main()
