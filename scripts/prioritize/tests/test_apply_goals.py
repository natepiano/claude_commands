from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from typing import final
from unittest import mock


SCRIPTS_DIR = Path(__file__).parents[2]
PRIORITIZE_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(PRIORITIZE_DIR))
from prioritize import apply_goals, renumber, writer_lock


OLD_GOALS = """# Prioritization goals

## Current goals

1. `1 - Ship Hana`
2. `2 - Find Collaborators`
3. `3 - Seek Investors`
"""

UPDATED_GOALS = """# Prioritization goals

This complete note deliberately has no YAML frontmatter.

## Current goals

1. `1 - Ship Hana`
2. `2 - Find Collaborators`
"""

RUBRIC = {
    "alignment": "⭐⭐",
    "impact": "⭐⭐⭐⭐",
    "urgency": "⭐",
    "effort": "⭐⭐⭐",
}


def issue_text(goal: str) -> str:
    lines = ["---", "status: open", f'strategic_goal: "{goal}"']
    lines.extend(f'{key}: "{value}"' for key, value in RUBRIC.items())
    lines.extend(["---", "# Issue", "", "Evidence."])
    return "\n".join(lines) + "\n"


@final
class Fixture:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.vault = self.root / "vault"
        self.issues = self.vault / "issues"
        self.issues.mkdir(parents=True)
        self.goals = self.vault / "prioritization goals.md"
        self.goals.write_text(OLD_GOALS, encoding="utf-8")
        self.evidence = self.vault / "goal evidence.md"
        self.evidence.write_text("# Goal evidence\n", encoding="utf-8")
        self.scope = renumber.Scope(self.vault, self.issues, self.goals)
        self.runtime = apply_goals.Runtime(
            scope=self.scope,
            writer_lock_path=self.root / "state" / "writer.lock",
        )

    def add(self, name: str, goal: str) -> Path:
        path = self.issues / name
        path.write_text(issue_text(goal), encoding="utf-8")
        return path

    def canonicalize(self) -> None:
        renumber.apply_plan(renumber.build_plan(self.scope))

    def manifest(
        self,
        updated_content: str = UPDATED_GOALS,
        *,
        expected_hash: str | None = None,
    ) -> Path:
        current = renumber._read_source(self.goals)
        record = {
            "expected_goals_hash": expected_hash or current.digest,
            "evidence_hashes": {
                str(self.evidence): renumber._read_source(self.evidence).digest
            },
            "updated_content": updated_content,
        }
        path = self.root / "approved-goals.json"
        path.write_text(json.dumps(record), encoding="utf-8")
        return path

    def close(self) -> None:
        self.temporary.cleanup()


@final
class ApplyGoalsTests(unittest.TestCase):
    _fixture: Fixture | None = None

    @property
    def fixture(self) -> Fixture:
        if self._fixture is None:
            raise AssertionError("fixture is not initialized")
        return self._fixture

    def setUp(self) -> None:
        self._fixture = Fixture()

    def tearDown(self) -> None:
        fixture = self._fixture
        if fixture is not None:
            fixture.close()
        self._fixture = None

    def test_goal_wikilinks_match_their_displayed_text(self) -> None:
        linked_goals = OLD_GOALS.replace(
            "1 - Ship Hana", "1 - Ship [[hana|Hana]]"
        )
        _ = self.fixture.goals.write_text(linked_goals, encoding="utf-8")
        _ = self.fixture.add("linked-goal.md", "1 - Ship Hana")

        plan = renumber.build_plan(self.fixture.scope)

        self.assertEqual(plan.goals[0].value, "1 - Ship Hana")
        self.assertEqual(len(plan.valid_open), 1)
        self.assertEqual(plan.needs_prioritization, ())

    def test_dry_run_is_default_and_writes_nothing(self) -> None:
        issue = self.fixture.add("ship.md", "1 - Ship Hana")
        manifest = self.fixture.manifest()
        goals_before = self.fixture.goals.read_bytes()
        issue_before = issue.read_bytes()
        output = io.StringIO()

        with mock.patch.object(
            apply_goals, "PRODUCTION_SCOPE", self.fixture.scope
        ):
            with contextlib.redirect_stdout(output):
                result = apply_goals.main([str(manifest)])

        self.assertEqual(result, 0)
        self.assertEqual(self.fixture.goals.read_bytes(), goals_before)
        self.assertEqual(issue.read_bytes(), issue_before)
        self.assertIn("dry-run (no files written)", output.getvalue())

    def test_apply_replaces_complete_note_reranks_and_preserves_mode(self) -> None:
        valid = self.fixture.add("ship.md", "1 - Ship Hana")
        invalidated = self.fixture.add("investors.md", "3 - Seek Investors")
        self.fixture.canonicalize()
        os.chmod(self.fixture.goals, 0o640)
        manifest = self.fixture.manifest()

        plan = apply_goals.apply_manifest(manifest, self.fixture.runtime)

        self.assertTrue(plan.changes_content)
        self.assertEqual(self.fixture.goals.read_text(encoding="utf-8"), UPDATED_GOALS)
        self.assertEqual(stat.S_IMODE(self.fixture.goals.stat().st_mode), 0o640)
        valid_content = valid.read_text(encoding="utf-8")
        self.assertIn("backlog_score: 13", valid_content)
        self.assertIn("backlog_rank: 1", valid_content)
        invalidated_content = invalidated.read_text(encoding="utf-8")
        self.assertIn('strategic_goal: "3 - Seek Investors"', invalidated_content)
        self.assertNotIn("backlog_score:", invalidated_content)
        self.assertNotIn("backlog_rank:", invalidated_content)
        self.assertEqual(renumber.build_plan(self.fixture.scope).changes, ())
        self.assertFalse(writer_lock.lock_is_held(self.fixture.runtime.writer_lock_path))

    def test_stale_full_note_hash_rejects_without_writes(self) -> None:
        issue = self.fixture.add("ship.md", "1 - Ship Hana")
        self.fixture.canonicalize()
        manifest = self.fixture.manifest()
        external_goals = OLD_GOALS + "\nExternal edit.\n"
        self.fixture.goals.write_text(external_goals, encoding="utf-8")
        issue_before = issue.read_bytes()

        with self.assertRaisesRegex(apply_goals.GoalsApplyError, "changed after approval"):
            apply_goals.apply_manifest(manifest, self.fixture.runtime)

        self.assertEqual(self.fixture.goals.read_text(encoding="utf-8"), external_goals)
        self.assertEqual(issue.read_bytes(), issue_before)

    def test_changed_goal_evidence_rejects_without_writes(self) -> None:
        issue = self.fixture.add("ship.md", "1 - Ship Hana")
        manifest = self.fixture.manifest()
        issue_before = issue.read_bytes()
        goals_before = self.fixture.goals.read_bytes()
        self.fixture.evidence.write_text(
            "# Changed goal evidence\n", encoding="utf-8"
        )

        with self.assertRaisesRegex(
            apply_goals.GoalsApplyError, "goal evidence changed"
        ):
            apply_goals.apply_manifest(manifest, self.fixture.runtime)

        self.assertEqual(issue.read_bytes(), issue_before)
        self.assertEqual(self.fixture.goals.read_bytes(), goals_before)

    def test_invalid_or_noncontiguous_goal_domains_are_rejected(self) -> None:
        invalid = """# Goals

## Current goals

1. `1 - Ship Hana`
2. `3 - Seek Investors`
"""
        manifest = self.fixture.manifest(invalid)

        with self.assertRaisesRegex(apply_goals.GoalsApplyError, "must begin with 2 -"):
            apply_goals.prepare_plan(manifest, self.fixture.scope)

        self.assertEqual(self.fixture.goals.read_text(encoding="utf-8"), OLD_GOALS)

    def test_final_validation_failure_rolls_back_goals_and_generated_fields(self) -> None:
        valid = self.fixture.add("ship.md", "1 - Ship Hana")
        invalidated = self.fixture.add("investors.md", "3 - Seek Investors")
        self.fixture.canonicalize()
        manifest = self.fixture.manifest()
        originals = {
            self.fixture.goals: self.fixture.goals.read_bytes(),
            valid: valid.read_bytes(),
            invalidated: invalidated.read_bytes(),
        }

        with mock.patch.object(
            apply_goals,
            "_validate_final_state",
            side_effect=apply_goals.GoalsApplyError("injected final failure"),
        ):
            with self.assertRaisesRegex(
                apply_goals.GoalsApplyError, "injected final failure"
            ):
                apply_goals.apply_manifest(manifest, self.fixture.runtime)

        for path, original in originals.items():
            self.assertEqual(path.read_bytes(), original)
        self.assertFalse(writer_lock.lock_is_held(self.fixture.runtime.writer_lock_path))

    def test_rollback_does_not_overwrite_a_concurrent_user_edit(self) -> None:
        issue = self.fixture.add("ship.md", "1 - Ship Hana")
        self.fixture.canonicalize()
        manifest = self.fixture.manifest()
        external = UPDATED_GOALS + "\nConcurrent user note.\n"

        def edit_then_fail(
            _plan: apply_goals.GoalsPlan,
            _scope: renumber.Scope,
            _paths: tuple[Path, ...],
        ) -> None:
            self.fixture.goals.write_text(external, encoding="utf-8")
            raise apply_goals.GoalsApplyError("injected concurrent edit")

        with mock.patch.object(
            apply_goals, "_validate_final_state", side_effect=edit_then_fail
        ):
            with self.assertRaisesRegex(
                apply_goals.GoalsApplyError, "rollback warnings"
            ):
                apply_goals.apply_manifest(manifest, self.fixture.runtime)

        self.assertEqual(self.fixture.goals.read_text(encoding="utf-8"), external)
        self.assertIn("backlog_score: 15", issue.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
