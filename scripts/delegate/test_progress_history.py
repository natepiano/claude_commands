#!/usr/bin/env python3
"""Integration tests for the durable plan-delegate progress history."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import cast, override


SCRIPT = Path(__file__).with_name("progress_history.py")
FINDINGS = Path(__file__).with_name("findings.py")


class ProgressHistoryTests(unittest.TestCase):
    temporary: tempfile.TemporaryDirectory[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    history_dir: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    working_dir: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    config_file: Path  # pyright: ignore[reportUninitializedInstanceVariable]

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
        """Point main-agent detection at the temporary home, not this machine's.

        Every window opening re-detects the orchestrator, and left alone that
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
        environment["TZ"] = "UTC"
        environment["PLAN_DELEGATE_NOW_EPOCH"] = str(at)
        environment["PLAN_DELEGATE_PASS_OWNER"] = "launcher"
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
        environment["TZ"] = "UTC"
        environment["PLAN_DELEGATE_NOW_EPOCH"] = str(at)
        environment["PLAN_DELEGATE_PASS_OWNER"] = "launcher"
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
                "| Stage | Main           | Delegate        | Start    | Elapsed  | Result  |",
                "| ----- | -------------- | --------------- | -------- | -------- | ------- |",
                "| Fix 2 | gpt-main xhigh | gpt-called high | 05:33:30 | 00:01:30 | running |",
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
                "add plan",
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
        self.assertEqual(state["project_start_source"], "plan_git")
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
        environment["TZ"] = "UTC"
        environment["PLAN_DELEGATE_NOW_EPOCH"] = str(at)
        _ = environment.pop("PLAN_DELEGATE_PASS_OWNER", None)
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

    def table_rows(self, rendered: str, header: list[str]) -> list[list[str]]:
        rows = [
            [cell.strip() for cell in line.removeprefix("| ").removesuffix(" |").split(" | ")]
            for line in rendered.splitlines()
            if line.startswith("| ") and set(line) - set("|-: ")
        ]
        return rows[rows.index(header) + 1 :]

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
        stage_rows = self.table_rows(
            header,
            ["Stage", "Main", "Delegate", "Start", "Elapsed", "Result"],
        )
        self.assertEqual(
            [(row[0], row[4], row[5]) for row in stage_rows],
            [
                ("Impl", "00:03:10", "done"),
                ("Review 1", "00:01:20", "2 found"),
                ("Fix 1", "00:02:40", "2 landed"),
                ("Review 2", "00:00:40", "2 fixed"),
                ("Verification", "00:00:40", "pass"),
                ("Review 3", "00:00:40", "running"),
            ],
        )
        self.assertIn("▸ **Review 3 - checking the remaining plan against what shipped**", header)
        # The main agent ran verification itself, so that row has no delegate.
        self.assertEqual((stage_rows[4][1], stage_rows[4][2]), ("gpt-main xhigh", ""))
        self.assertEqual(stage_rows[0][1], "gpt-main xhigh")

    def write_agents_registry(self) -> None:
        """A registry under the temporary home, so arm-review resolves the same
        agent review.sh would without reading this machine's real assignments."""
        config = self.root / ".claude" / "config"
        config.mkdir(parents=True, exist_ok=True)
        _ = (config / "agents.conf").write_text(
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
        stage_rows = self.table_rows(
            header,
            ["Stage", "Main", "Delegate", "Start", "Elapsed", "Result"],
        )
        self.assertEqual(
            [(row[0], row[2], row[4], row[5]) for row in stage_rows],
            [
                ("Fix 2", "gpt-called high", "00:02:30", "running"),
                ("Review 1", "gpt-blind max", "00:01:00", "running (early)"),
            ],
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
            [("Fix 2", "done"), ("Review 1", "running")],
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
        """`start-run` detects the orchestrator once, and it can change after."""
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


    def test_the_header_names_the_phase_position_in_the_plan(self) -> None:
        """Worktree and branch say where the run is, not how much plan is left.

        The position is the ordinal of the heading the phase in flight actually
        occupies, looked up by the id `start-phase` named, off the same headings
        the project percentage derives from.
        """
        session_dir = self.start_run("position", 0)
        plan_path = self.working_dir / "docs" / "position.md"
        _ = plan_path.write_text(
            plan_path.read_text(encoding="utf-8")
            + "### Phase 1 — Shrunk archive form (`bbac234`)\n\n"
            + "### Phase 2 — Completed  · status: done\n\n"
            + "### Phase 3 — Live work  · status: todo\n\n"
            + "### Phase 4 — Waiting  · status: todo\n\n"
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
            "**bevy_hana_rubric - feature/rubric - phase 3 of 5**",
        )
        # The supplied 90 is advisory; two finished phases and a half-done third
        # of five is 50, and the position above counts the same headings.
        self.assertIn("| Project |  50 | ", header)
        self.assertIn("| 2 of 5 done |", header)

    def test_the_position_holds_while_the_phase_review_window_marks_it_done(
        self,
    ) -> None:
        """`/plan:phase_review` flips the phase to done before its checkpoint.

        For that whole window the phase in flight is also a finished phase, so
        counting finished phases and adding one names the phase after it. The
        position must keep naming the phase `start-phase` opened.
        """
        session_dir = self.start_run("position", 0)
        plan_path = self.working_dir / "docs" / "position.md"
        _ = plan_path.write_text(
            plan_path.read_text(encoding="utf-8")
            + "### Phase 1 — Shrunk archive form (`bbac234`)\n\n"
            + "### Phase 2 — Completed  · status: done\n\n"
            + "### Phase 3 — Live work  · status: done\n\n"
            + "### Phase 4 — Waiting  · status: todo\n\n"
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
            "closure",
            "--activity",
            "reviewing the remaining phases",
            at=80_000,
        )
        self.assertEqual(
            header.splitlines()[0],
            "**bevy_hana_rubric - feature/rubric - phase 3 of 5**",
        )


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


if __name__ == "__main__":
    _ = unittest.main()
