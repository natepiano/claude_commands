"""A review director's standing phase uses the existing run recorder."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

import unit_lookup
import units_table

# units_table imports the delegate readers through their script directory.
import phase_table


REPOSITORY = Path(__file__).resolve().parents[2]
PYTHON = REPOSITORY / "scripts/lib/py"
RECORDER = REPOSITORY / "scripts/delegate/progress_history.py"
PHASE_TITLE = "Keep review pools primed and curate memory"
PHASE_HEADING = f"### Phase 1 — {PHASE_TITLE} · status: todo"


class ReviewUnitPhaseTests(unittest.TestCase):
    root: Path = Path()
    history: Path = Path()
    worktree: Path = Path()
    plan: Path = Path()
    production: Path = Path()
    session: Path = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.history = self.root / "history"
        self.worktree = self.root / "production-review"
        self.worktree.mkdir()
        _ = (self.worktree / ".git").write_text(
            f"gitdir: {self.root / 'git/worktrees/review'}\n", encoding="utf-8",
        )
        self.production = self.root / "fixture-production.md"
        _ = self.production.write_text("\n".join([
            "# Production", "", "## Production Context", "",
            "- **Merge branch:** main",
            f"- **Showrunner checkout:** {self.root}",
            "- **Log:** production.log", "- **User zone:** UTC",
            "- **Review pool:** code", "", "## Units", "",
            "| Unit | Plan | Worktree | Branch | Port | Owns |",
            "| --- | --- | --- | --- | --- | --- |",
            f"| review-unit | review-plan.md | {self.worktree} | review | — | — |", "",
        ]), encoding="utf-8")
        self.plan = self.worktree / "review-plan.md"
        _ = self.plan.write_text(
            "# Review unit\n\n" +
            "> **Production: fixture** — unit `review-unit`; " +
            f"production doc `{self.production}`\n\n" +
            "## Delegation Context\n\n" +
            "- **Project:** Review fixture\n" +
            "- **Project started:** 2026-10-12T18:56:58.313+00:00\n\n" +
            f"## Phases\n\n{PHASE_HEADING}\n",
            encoding="utf-8",
        )
        self.session = self.history / "review_units/review-fixture"
        self.environment = {
            **os.environ,
            "HOME": str(self.root / "home"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "PLAN_DELEGATE_HISTORY_DIR": str(self.history),
            "REVIEW_POOL_STATE": str(self.root / "pools"),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
            "PHASE_TABLE_VAULT": str(self.root / "missing-vault/showrunners"),
            "CLAUDE_PYTHON": sys.executable,
            "CLAUDE_CODE_SESSION_ID": "fixture-review-director",
            "PYTHONDONTWRITEBYTECODE": "1",
            "TZ": "UTC",
        }
        _ = self.environment.pop("CODEX_THREAD_ID", None)
        self.enterContext(patch.dict(os.environ, self.environment, clear=True))

    def record(self, *arguments: str) -> None:
        result = subprocess.run(
            [str(PYTHON), str(RECORDER), *arguments],
            env=self.environment, capture_output=True, text=True,
            check=False, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def start_or_resume(self) -> None:
        # The fixture has no session transcript, so it names the director's
        # identity; a live director lets the recorder detect it.
        self.record(
            "start-run", "--session-dir", str(self.session),
            "--working-dir", str(self.worktree), "--plan-doc", str(self.plan),
            "--main-family", "claude", "--main-model", "claude-opus-4-6",
            "--main-effort", "high", "--main-session-id", "fixture-review-director",
        )
        self.record(
            "start-phase", "--session-dir", str(self.session),
            "--phase-id", "1", "--phase-title", PHASE_TITLE,
        )

    def phase_record(self) -> dict[str, object]:
        records = cast(dict[str, object], json.loads(phase_table.show_production(self.production)))
        return cast(dict[str, object], records["review-unit"])

    def events(self) -> list[dict[str, object]]:
        path = self.history / "runs/review-fixture.jsonl"
        return [cast(dict[str, object], json.loads(line))
                for line in path.read_text(encoding="utf-8").splitlines()]

    def test_recording_the_standing_phase_displays_it_and_marks_the_unit_running(self) -> None:
        self.assertEqual(units_table.phase_cell(self.phase_record()), "not started")
        self.assertIs(unit_lookup.run_state(self.worktree), unit_lookup.UnitState.STANDING_BY)

        self.start_or_resume()

        self.assertEqual(units_table.phase_cell(self.phase_record()), f"1 of 1 · {PHASE_TITLE}")
        self.assertIs(unit_lookup.run_state(self.worktree), unit_lookup.UnitState.RUNNING)
        self.assertIn(PHASE_HEADING, self.plan.read_text(encoding="utf-8"))
        self.assertFalse((self.session / "progress_timer").exists())
        self.assertFalse((self.root / "sessions").exists())

    def test_resuming_the_review_director_reuses_its_run_and_standing_phase(self) -> None:
        self.start_or_resume()
        first_events = self.events()

        self.start_or_resume()

        events = self.events()
        self.assertEqual(events, first_events)
        self.assertEqual(sum(event["event_type"] == "run_started" for event in events), 1)
        self.assertEqual(sum(event["event_type"] == "phase_started" for event in events), 1)
        self.assertEqual(list((self.history / "runs").glob("*.jsonl")),
                         [self.history / "runs/review-fixture.jsonl"])
        self.assertEqual(units_table.phase_cell(self.phase_record()), f"1 of 1 · {PHASE_TITLE}")
        self.assertIs(unit_lookup.run_state(self.worktree), unit_lookup.UnitState.RUNNING)

    def test_shutdown_closes_the_run_and_phase_while_the_plan_heading_stays_todo(self) -> None:
        self.start_or_resume()

        self.record("finish-run", "--session-dir", str(self.session), "--status", "completed")

        self.assertIsNone(self.phase_record()["current"])
        self.assertIs(unit_lookup.run_state(self.worktree), unit_lookup.UnitState.RUN_FINISHED)
        events = self.events()
        self.assertEqual([event["event_type"] for event in events[-2:]],
                         ["phase_finished", "run_finished"])
        self.assertTrue(all(event["status"] == "completed" for event in events[-2:]))
        self.assertIn(PHASE_HEADING, self.plan.read_text(encoding="utf-8"))


if __name__ == "__main__":
    _ = unittest.main()
