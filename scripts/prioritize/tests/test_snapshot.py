from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast, final
from unittest import mock


SCRIPTS_DIR = Path(__file__).parents[2]
PRIORITIZE_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(PRIORITIZE_DIR))
from prioritize import snapshot


@final
class SnapshotTests(unittest.TestCase):
    def test_domain_scalar_accepts_yaml_string_styles(self) -> None:
        self.assertEqual(snapshot.parse_domain_scalar("⭐⭐⭐⭐"), "⭐⭐⭐⭐")
        self.assertEqual(snapshot.parse_domain_scalar("'⭐⭐⭐⭐'"), "⭐⭐⭐⭐")
        self.assertEqual(snapshot.parse_domain_scalar('"⭐⭐⭐⭐"'), "⭐⭐⭐⭐")

    def test_completeness_uses_canonical_rubric_domains(self) -> None:
        candidate = {
            "goals": ["1 - Ship Hana"],
            "issues": [
                {
                    "path": "issues/example.md",
                    "frontmatter": "valid",
                    "status": "open",
                    "strategic_goal": "1 - Ship Hana",
                    "impact": "9 - Canonical",
                }
            ],
        }
        canonical = {"impact": ("9 - Canonical",)}

        with mock.patch.object(snapshot.renumber, "RUBRIC_DOMAINS", canonical):
            errors = list(snapshot.completeness_errors(candidate))

        self.assertEqual(errors, [])

    def test_goal_wikilinks_are_snapshotted_as_displayed_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            issue = root / "issue.md"
            _ = issue.write_text(
                "---\nstatus: open\n"
                + 'strategic_goal: "1 - Ship [[hana|Hana]]"\n'
                + "---\n",
                encoding="utf-8",
            )
            goals = root / "prioritization goals.md"
            _ = goals.write_text(
                "## Current goals\n\n1. `1 - Ship [[hana|Hana]]`\n",
                encoding="utf-8",
            )

            raw_values, raw_state = snapshot.frontmatter_values(issue)
            with mock.patch.object(snapshot, "GOALS_FILE", goals):
                raw_goals = snapshot.current_goals()

        values = cast(dict[str, object], raw_values)
        state = cast(object, raw_state)
        current_goals = cast(list[str], raw_goals)
        self.assertEqual(state, "valid")
        self.assertEqual(values["strategic_goal"], "1 - Ship Hana")
        self.assertEqual(current_goals, ["1 - Ship Hana"])


if __name__ == "__main__":
    unittest.main()
