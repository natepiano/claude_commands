from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo


SCRIPTS_DIR = Path(__file__).parents[2]
PRIORITIZE_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(PRIORITIZE_DIR))
from prioritize import renumber


GOALS = """# prioritization goals

## Current goals

1. `1 - Ship Hana`
2. `2 - Find Collaborators`
3. `3 - Seek Investors`
"""

DEFAULT_VALUES = {
    "backlog_goal": "1 - Ship Hana",
    "backlog_alignment": "⭐⭐",
    "backlog_impact": "⭐⭐⭐⭐",
    "backlog_urgency": "⭐",
    "backlog_effort": "⭐⭐⭐",
}


def issue_text(
    *,
    status: str = "open",
    values: dict[str, str] | None = None,
    generated: tuple[int, int] | None = None,
    extra_frontmatter: str = "",
    body: str = "# Issue\n",
    newline: str = "\n",
) -> str:
    selected = DEFAULT_VALUES if values is None else values
    lines = ["---", f"status: {status}"]
    for key, value in selected.items():
        lines.append(f'{key}: "{value}"')
    if extra_frontmatter:
        lines.extend(extra_frontmatter.rstrip("\n").split("\n"))
    if generated is not None:
        lines.extend(
            [
                f"backlog_score: {generated[0]}",
                f"backlog_rank: {generated[1]}",
            ]
        )
    lines.extend(["---", body.rstrip("\n")])
    return newline.join(lines) + newline


class VaultFixture:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.vault = Path(self.temporary.name)
        self.issues = self.vault / "issues"
        self.issues.mkdir()
        self.goals = self.vault / "prioritization goals.md"
        self.goals.write_text(GOALS, encoding="utf-8")
        self.scope = renumber.Scope(self.vault, self.issues, self.goals)

    def add(self, name: str, content: str) -> Path:
        path = self.issues / name
        path.write_bytes(content.encode("utf-8"))
        return path

    def close(self) -> None:
        self.temporary.cleanup()


class RenumberTests(unittest.TestCase):
    fixture: VaultFixture  # pyright: ignore[reportUninitializedInstanceVariable]

    def setUp(self) -> None:
        self.fixture = VaultFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def test_score_uses_settled_formula(self) -> None:
        goal_source = renumber._read_source(self.fixture.goals)
        goal = renumber.parse_goals(goal_source)[0]
        values = {
            "backlog_alignment": 2,
            "backlog_impact": 4,
            "backlog_urgency": 1,
            "backlog_effort": 3,
        }

        self.assertEqual(renumber.calculate_score(goal, values), 15)

        values["backlog_alignment"] = 1
        self.assertEqual(renumber.calculate_score(goal, values), 11)

    def test_urgency_scores_as_tiers(self) -> None:
        goal_source = renumber._read_source(self.fixture.goals)
        goal = renumber.parse_goals(goal_source)[0]
        values = {
            "backlog_alignment": 1,
            "backlog_impact": 1,
            "backlog_urgency": 1,
            "backlog_effort": 1,
        }

        scores: list[int] = []
        for stars in range(1, 6):
            values["backlog_urgency"] = stars
            scores.append(renumber.calculate_score(goal, values))

        self.assertEqual(scores, [4, 6, 8, 24, 74])

    def test_five_star_urgency_outranks_every_lower_rating(self) -> None:
        """The weakest five-star issue must still beat the strongest four-star one."""
        goal_source = renumber._read_source(self.fixture.goals)
        goals = renumber.parse_goals(goal_source)

        weakest_override = renumber.calculate_score(
            goals[-1],
            {
                "backlog_alignment": 1,
                "backlog_impact": 1,
                "backlog_urgency": 5,
                "backlog_effort": 5,
            },
        )
        strongest_below = max(
            renumber.calculate_score(
                goals[0],
                {
                    "backlog_alignment": 5,
                    "backlog_impact": 5,
                    "backlog_urgency": stars,
                    "backlog_effort": 1,
                },
            )
            for stars in range(1, 5)
        )

        self.assertGreater(weakest_override, strongest_below)

    def test_goal_bonus_tracks_the_ordered_goal_count(self) -> None:
        self.fixture.goals.write_text(
            GOALS + "4. `4 - Build Community`\n", encoding="utf-8"
        )

        goals = renumber.parse_goals(renumber._read_source(self.fixture.goals))

        self.assertEqual([goal.bonus for goal in goals], [6, 4, 2, 0])

    def test_malformed_domain_is_unranked_and_reported(self) -> None:
        values = dict(DEFAULT_VALUES)
        values["backlog_impact"] = "⭐⭐⭐⭐⭐⭐"
        path = self.fixture.add(
            "invalid.md", issue_text(values=values, generated=(99, 1))
        )

        plan = renumber.build_plan(self.fixture.scope)

        self.assertEqual(len(plan.valid_open), 0)
        self.assertEqual(len(plan.needs_prioritization), 1)
        self.assertIn("invalid backlog_impact", plan.needs_prioritization[0].problems[0])
        self.assertEqual(len(plan.changes), 1)

        renumber.apply_plan(plan)
        content = path.read_text(encoding="utf-8")
        self.assertNotIn("backlog_score:", content)
        self.assertNotIn("backlog_rank:", content)

    def test_domain_values_accept_yaml_string_styles(self) -> None:
        plain = issue_text()
        single_quoted = issue_text()
        for key, value in DEFAULT_VALUES.items():
            plain = plain.replace(f'{key}: "{value}"', f"{key}: {value}")
            single_quoted = single_quoted.replace(
                f'{key}: "{value}"', f"{key}: '{value}'"
            )
        self.fixture.add("plain.md", plain)
        self.fixture.add("single-quoted.md", single_quoted)

        plan = renumber.build_plan(self.fixture.scope)

        self.assertEqual(len(plan.valid_open), 2)
        self.assertEqual(plan.needs_prioritization, ())

    def test_valid_open_replaces_generated_block_values_completely(self) -> None:
        path = self.fixture.add(
            "block-generated.md",
            issue_text(
                extra_frontmatter=(
                    "backlog_score:\n"
                    "  - 999\n"
                    "backlog_rank:\n"
                    "- 77"
                )
            ),
        )

        plan = renumber.build_plan(self.fixture.scope)
        renumber.apply_plan(plan)

        content = path.read_text(encoding="utf-8")
        self.assertIn("backlog_score: 15\n", content)
        self.assertIn("backlog_rank: 1\n", content)
        self.assertNotIn("  - 999", content)
        self.assertNotIn("- 77", content)
        self.assertEqual(renumber.build_plan(self.fixture.scope).changes, ())

    def test_invalid_open_removes_generated_block_values_completely(self) -> None:
        values = dict(DEFAULT_VALUES)
        del values["backlog_effort"]
        path = self.fixture.add(
            "invalid-block-generated.md",
            issue_text(
                values=values,
                extra_frontmatter=(
                    "backlog_score: |\n"
                    "  999\n"
                    "backlog_rank:\n"
                    "  - 77"
                ),
            ),
        )

        plan = renumber.build_plan(self.fixture.scope)
        self.assertEqual(len(plan.needs_prioritization), 1)
        renumber.apply_plan(plan)

        content = path.read_text(encoding="utf-8")
        self.assertNotIn("backlog_score:", content)
        self.assertNotIn("backlog_rank:", content)
        self.assertNotIn("  999", content)
        self.assertNotIn("  - 77", content)
        self.assertEqual(renumber.build_plan(self.fixture.scope).changes, ())

    def test_dry_run_is_default_and_writes_nothing(self) -> None:
        path = self.fixture.add("issue.md", issue_text())
        original = path.read_bytes()
        output = io.StringIO()

        with mock.patch.object(renumber, "PRODUCTION_SCOPE", self.fixture.scope):
            with contextlib.redirect_stdout(output):
                result = renumber.main([])

        self.assertEqual(result, 0)
        self.assertEqual(path.read_bytes(), original)
        self.assertIn("dry-run (no files written)", output.getvalue())
        self.assertIn("Run with --apply", output.getvalue())

    def test_apply_preserves_unrelated_frontmatter_and_body(self) -> None:
        original = issue_text(
            generated=(999, 999),
            extra_frontmatter='custom: "keep exactly"\nitems:\n  - alpha',
            body="# Exact body\n\nText with  two spaces.  ",
            newline="\r\n",
        )
        path = self.fixture.add("preserve.md", original)

        plan = renumber.build_plan(self.fixture.scope)
        renumber.apply_plan(plan)

        expected = original.replace("backlog_score: 999", "backlog_score: 15").replace(
            "backlog_rank: 999", "backlog_rank: 1"
        )
        self.assertEqual(path.read_bytes(), expected.encode("utf-8"))

    @unittest.skipUnless(sys.platform == "darwin", "requires macOS creation dates")
    def test_apply_preserves_creation_time_and_modified_calendar_date(self) -> None:
        path = self.fixture.add("dates.md", issue_text())
        subprocess.run(
            ["/usr/bin/SetFile", "-d", "05/19/2024 12:34:56", str(path)],
            check=True,
            capture_output=True,
            text=True,
        )
        new_york = ZoneInfo("America/New_York")
        modified_seconds = int(
            datetime(2024, 6, 3, 23, 59, 59, tzinfo=new_york).timestamp()
        )
        modified_ns = (modified_seconds * 1_000_000_000) + 999_500_000
        before_set = path.stat()
        os.utime(
            path,
            ns=(before_set.st_atime_ns, modified_ns),
            follow_symlinks=False,
        )
        before = path.stat()
        original = path.read_bytes()

        plan = renumber.build_plan(self.fixture.scope)
        renumber.apply_plan(plan)

        after = path.stat()
        self.assertNotEqual(path.read_bytes(), original)
        # In-place edit keeps the same inode; that is what preserves the
        # creation date without a SetFile call.
        self.assertEqual(after.st_ino, before.st_ino)
        self.assertEqual(int(after.st_birthtime), int(before.st_birthtime))
        observed_delta = after.st_mtime_ns - before.st_mtime_ns
        self.assertNotEqual(observed_delta, 0)
        self.assertLessEqual(
            abs(abs(observed_delta) - 1_000_000),
            1_024,
        )
        self.assertEqual(
            datetime.fromtimestamp(after.st_mtime, new_york).date(),
            datetime.fromtimestamp(before.st_mtime, new_york).date(),
        )

    def test_second_run_is_idempotent(self) -> None:
        self.fixture.add("issue.md", issue_text())

        first = renumber.build_plan(self.fixture.scope)
        self.assertEqual(len(first.changes), 1)
        renumber.apply_plan(first)

        second = renumber.build_plan(self.fixture.scope)
        self.assertEqual(second.changes, ())

    def test_missing_issue_does_not_block_valid_ranking(self) -> None:
        missing_values = dict(DEFAULT_VALUES)
        del missing_values["backlog_effort"]
        missing = self.fixture.add(
            "missing.md", issue_text(values=missing_values, generated=(70, 9))
        )
        valid = self.fixture.add("valid.md", issue_text())

        plan = renumber.build_plan(self.fixture.scope)

        self.assertEqual(len(plan.valid_open), 1)
        self.assertEqual(plan.valid_open[0].source.path, valid)
        self.assertEqual(plan.valid_open[0].assigned_rank, 1)
        self.assertEqual(len(plan.needs_prioritization), 1)
        self.assertIn("missing backlog_effort", plan.needs_prioritization[0].problems)

        renumber.apply_plan(plan)
        self.assertIn("backlog_rank: 1", valid.read_text(encoding="utf-8"))
        self.assertNotIn("backlog_rank:", missing.read_text(encoding="utf-8"))

    def test_closed_issue_loses_generated_fields_but_keeps_rubric(self) -> None:
        path = self.fixture.add(
            "closed.md", issue_text(status="closed", generated=(28, 1))
        )

        plan = renumber.build_plan(self.fixture.scope)
        renumber.apply_plan(plan)
        content = path.read_text(encoding="utf-8")

        self.assertIn('backlog_goal: "1 - Ship Hana"', content)
        self.assertIn('backlog_impact: "⭐⭐⭐⭐"', content)
        self.assertNotIn("backlog_score:", content)
        self.assertNotIn("backlog_rank:", content)

    def test_ranks_are_dense_and_ties_preserve_unique_existing_order(self) -> None:
        # Same score, with existing order intentionally opposite path order.
        zulu = self.fixture.add("zulu.md", issue_text(generated=(15, 2)))
        alpha = self.fixture.add("alpha.md", issue_text(generated=(15, 7)))

        lower_values = dict(DEFAULT_VALUES)
        lower_values["backlog_impact"] = "⭐"
        lower = self.fixture.add("lower.md", issue_text(values=lower_values))

        plan = renumber.build_plan(self.fixture.scope)
        ordered = sorted(plan.valid_open, key=lambda issue: issue.assigned_rank or 0)

        self.assertEqual([issue.source.path for issue in ordered], [zulu, alpha, lower])
        self.assertEqual([issue.assigned_rank for issue in ordered], [1, 2, 3])

        renumber.apply_plan(plan)
        ranks = []
        for path in (zulu, alpha, lower):
            text = path.read_text(encoding="utf-8")
            rank_line = next(
                line for line in text.splitlines() if line.startswith("backlog_rank:")
            )
            ranks.append(int(rank_line.partition(":")[2]))
        self.assertEqual(sorted(ranks), [1, 2, 3])

    def test_new_ties_fall_back_to_path(self) -> None:
        bravo = self.fixture.add("bravo.md", issue_text())
        alpha = self.fixture.add("alpha.md", issue_text())

        plan = renumber.build_plan(self.fixture.scope)
        ordered = sorted(plan.valid_open, key=lambda issue: issue.assigned_rank or 0)

        self.assertEqual([issue.source.path for issue in ordered], [alpha, bravo])

    def test_open_dependency_ranks_immediately_before_dependent(self) -> None:
        prerequisite_values = dict(DEFAULT_VALUES)
        prerequisite_values["backlog_impact"] = "⭐"
        prerequisite = self.fixture.add(
            "prerequisite.md", issue_text(values=prerequisite_values)
        )
        dependent = self.fixture.add(
            "dependent.md",
            issue_text(extra_frontmatter='depends_on: ["[[prerequisite]]"]'),
        )

        plan = renumber.build_plan(self.fixture.scope)
        ordered = sorted(plan.valid_open, key=lambda issue: issue.assigned_rank or 0)

        self.assertEqual([issue.source.path for issue in ordered], [prerequisite, dependent])

    def test_multiple_dependencies_place_dependent_after_the_last_one(self) -> None:
        low_values = dict(DEFAULT_VALUES)
        low_values["backlog_impact"] = "⭐"
        first = self.fixture.add("first.md", issue_text(values=low_values))
        second = self.fixture.add("second.md", issue_text(values=low_values))
        dependent = self.fixture.add(
            "dependent.md",
            issue_text(
                extra_frontmatter=(
                    "depends_on:\n"
                    '  - "[[first]]"\n'
                    '  - "[[second]]"'
                )
            ),
        )

        plan = renumber.build_plan(self.fixture.scope)
        ordered = sorted(plan.valid_open, key=lambda issue: issue.assigned_rank or 0)

        self.assertEqual([issue.source.path for issue in ordered], [first, second, dependent])

    def test_closed_dependencies_are_ignored(self) -> None:
        self.fixture.add("closed.md", issue_text(status="closed"))
        dependent = self.fixture.add(
            "dependent.md",
            issue_text(extra_frontmatter='depends_on: ["[[closed]]"]'),
        )

        plan = renumber.build_plan(self.fixture.scope)

        self.assertEqual(len(plan.valid_open), 1)
        self.assertEqual(plan.valid_open[0].source.path, dependent)
        self.assertEqual(plan.valid_open[0].assigned_rank, 1)

    def test_unknown_dependency_is_unranked_and_reported(self) -> None:
        dependent = self.fixture.add(
            "dependent.md",
            issue_text(extra_frontmatter='depends_on: ["[[missing]]"]'),
        )

        plan = renumber.build_plan(self.fixture.scope)

        self.assertEqual(plan.valid_open, ())
        self.assertEqual(plan.needs_prioritization[0].source.path, dependent)
        self.assertIn(
            "depends_on issue not found: [[missing]]",
            plan.needs_prioritization[0].problems,
        )

    def test_open_dependency_cycle_prevents_ranking(self) -> None:
        self.fixture.add(
            "first.md", issue_text(extra_frontmatter='depends_on: ["[[second]]"]')
        )
        self.fixture.add(
            "second.md", issue_text(extra_frontmatter='depends_on: ["[[first]]"]')
        )

        with self.assertRaisesRegex(
            renumber.PlanningError, "open depends_on cycle prevents ranking"
        ):
            _ = renumber.build_plan(self.fixture.scope)

    def test_apply_refuses_a_file_changed_after_discovery(self) -> None:
        path = self.fixture.add("issue.md", issue_text())
        plan = renumber.build_plan(self.fixture.scope)
        path.write_text(path.read_text(encoding="utf-8") + "external edit\n")

        with self.assertRaises(renumber.ConcurrentChangeError):
            renumber.apply_plan(plan)

        self.assertTrue(path.read_text(encoding="utf-8").endswith("external edit\n"))

    def test_apply_rolls_back_files_written_before_a_failure(self) -> None:
        first = self.fixture.add("first.md", issue_text())
        second = self.fixture.add("second.md", issue_text())
        originals = {first: first.read_bytes(), second: second.read_bytes()}
        plan = renumber.build_plan(self.fixture.scope)
        atomic_write = renumber._atomic_write
        call_count = 0

        def fail_second_write(path: Path, content: bytes, mode: int) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise OSError("injected write failure")
            atomic_write(path, content, mode)

        with mock.patch.object(renumber, "_atomic_write", fail_second_write):
            with self.assertRaises(renumber.ApplyError):
                renumber.apply_plan(plan)

        self.assertEqual(first.read_bytes(), originals[first])
        self.assertEqual(second.read_bytes(), originals[second])

    def test_apply_detects_concurrent_change_to_unwritten_issue(self) -> None:
        stable = self.fixture.add("alpha.md", issue_text(generated=(15, 1)))
        changed = self.fixture.add("zulu.md", issue_text())
        changed_original = changed.read_bytes()
        stable_original = stable.read_bytes()
        plan = renumber.build_plan(self.fixture.scope)
        self.assertEqual([change.source.path for change in plan.changes], [changed])
        atomic_write = renumber._atomic_write

        def write_then_edit_unwritten(path: Path, content: bytes, mode: int) -> None:
            atomic_write(path, content, mode)
            stable.write_bytes(stable_original + b"concurrent edit\n")

        with mock.patch.object(
            renumber, "_atomic_write", write_then_edit_unwritten
        ):
            with self.assertRaises(renumber.ConcurrentChangeError):
                renumber.apply_plan(plan)

        self.assertEqual(changed.read_bytes(), changed_original)
        self.assertEqual(stable.read_bytes(), stable_original + b"concurrent edit\n")

    def test_check_reports_whether_changes_are_needed(self) -> None:
        self.fixture.add("issue.md", issue_text())
        output = io.StringIO()

        with mock.patch.object(renumber, "PRODUCTION_SCOPE", self.fixture.scope):
            with contextlib.redirect_stdout(output):
                self.assertEqual(renumber.main(["--check"]), 1)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(renumber.main(["--apply"]), 0)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(renumber.main(["--check"]), 0)

    def test_require_complete_fails_only_for_unassessed_open_issues(self) -> None:
        missing_values = dict(DEFAULT_VALUES)
        del missing_values["backlog_effort"]
        self.fixture.add("missing.md", issue_text(values=missing_values))

        with mock.patch.object(renumber, "PRODUCTION_SCOPE", self.fixture.scope):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(renumber.main([]), 0)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(renumber.main(["--require-complete"]), 1)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    renumber.main(["--check", "--require-complete"]), 1
                )

        self.fixture.add("closed.md", issue_text(status="closed", values={}))
        (self.fixture.issues / "missing.md").unlink()
        with mock.patch.object(renumber, "PRODUCTION_SCOPE", self.fixture.scope):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(renumber.main(["--require-complete"]), 0)


if __name__ == "__main__":
    unittest.main()
