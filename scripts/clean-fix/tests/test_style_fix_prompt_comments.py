#!/usr/bin/env python3
"""Regression checks for documentation policy in style-fix prompts."""

import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "style-fix-worktrees.sh"


class StyleFixPromptCommentPolicyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SCRIPT.read_text()

    def test_apply_prompt_allows_direct_comment_findings(self) -> None:
        self.assertIn(
            "If a finding's governing style rule or Recommended pattern directly requires",
            self.source,
        )
        self.assertIn(
            "Do not mark a comment-focused finding Partially applied or Skipped",
            self.source,
        )

    def test_verify_prompt_rejects_comment_preservation_skip(self) -> None:
        self.assertIn(
            "Comment preservation is not a legitimate skip reason",
            self.source,
        )
        self.assertIn(
            "For a comment-focused finding, apply the required comment edits",
            self.source,
        )

    def test_obsolete_absolute_policy_is_absent(self) -> None:
        self.assertNotIn(
            "Each recommended pattern in a finding is a structural code change",
            self.source,
        )
        self.assertNotIn(
            "The user reviews comment changes during",
            self.source,
        )


if __name__ == "__main__":
    unittest.main()
