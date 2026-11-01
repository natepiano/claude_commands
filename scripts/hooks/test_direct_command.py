"""The unit command and the hooks that re-read it."""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from typing import cast, override


REPO = Path(__file__).resolve().parents[2]


class CommandStubTests(unittest.TestCase):
    def test_direct_command_is_a_regular_file_with_direct_usage(self) -> None:
        command = REPO / "commands/unit/direct.md"

        self.assertTrue(command.is_file())
        self.assertFalse(command.is_symlink())
        usage_lines = [
            line for line in command.read_text().splitlines()
            if line.startswith("**Usage:**")
        ]
        self.assertEqual(len(usage_lines), 1)
        self.assertTrue(usage_lines[0].startswith("**Usage:** `/unit:direct "))

    def test_hooks_reread_the_direct_command(self) -> None:
        for name in (
            "session-start-direct-resume.py",
            "stop-direct-continue.py",
        ):
            with self.subTest(name=name):
                text = (REPO / "scripts/hooks" / name).read_text()
                command_paths = cast(
                    list[str],
                    re.findall(r"~/\.claude/(commands/[\w/]+\.md)", text),
                )
                self.assertEqual(set(command_paths), {"commands/unit/direct.md"})


class PhaseStyleReviewTests(unittest.TestCase):
    review: str = ""

    @override
    def setUp(self) -> None:
        self.review = (REPO / "docs/direct/dual_review.md").read_text()

    def review_section(self, tag: str) -> str:
        return self.review.split(f"<{tag}>\n", 1)[1].split(f"</{tag}>", 1)[0]

    def test_the_contract_reviewer_owns_style_and_reports_through_synthesis(self) -> None:
        prompt = self.review_section("ReviewPromptContract")
        main = self.review_section("DualReview")
        style = (REPO / "commands/unit/style_review.md").read_text()

        self.assertIn("dispatch the `contract`", prompt)
        self.assertIn("--style-since <phase_baseline commit>", prompt)
        self.assertIn("runner completes Off", prompt)
        self.assertIn("normal review summary for <Synthesize/>", prompt)
        self.assertIn("or errored report leaves it owed", prompt)
        self.assertNotIn("load-rust-style.sh", main)
        self.assertNotIn("apply <StyleReview/>", main)
        self.assertIn("config/review/style.md", style)
        self.assertIn("<Synthesize/> records", style)
        self.assertIn("not write the ledger", style)

    def test_cancellation_keeps_style_owed_for_the_next_reviewer(self) -> None:
        prompt = self.review_section("ReviewPromptContract")
        main = self.review_section("DualReview")
        closure = self.review_section("ClosureReview")

        self.assertIn("The phase style review starts **owed**", prompt)
        self.assertIn("option on the next closure", prompt)
        self.assertIn("only when a reviewer returns the report", prompt)
        self.assertIn("A canceled or failed dispatch", prompt)
        self.assertIn("leaves it owed, even if an\nartifact was written", prompt)
        self.assertIn("phase cannot converge while style is owed", prompt)
        self.assertIn("owed assignment\nalone", prompt)
        self.assertIn("Any owed style assignment follows\n   <ReviewPromptContract/> on the next reviewer dispatch", main)
        self.assertIn("style assignment from <ReviewPromptContract/> when still owed", closure)
        self.assertIn("complete phase diff and baseline", closure)
        self.assertIn("Outside an owed style assignment", closure)
        self.assertNotIn("On pass 1, append this assignment", prompt)

    def test_style_directory_comes_from_the_assignment_alone(self) -> None:
        review = self.review
        style = (REPO / "commands/unit/style_review.md").read_text()
        as_built = (REPO / "docs/as-built/phase-checks.md").read_text()

        self.assertIn("under the phase's `style/` folder", review)
        self.assertIn("style_review.py prepare", style)
        self.assertIn("phase paths come from `<ReviewPromptContract/>`", as_built)
        self.assertNotIn("a run supplies its session directory", style)
        self.assertNotIn("A run passes `${SESSION_DIR}`", as_built)
        self.assertNotIn("<SESSION_DIR>/style/", style + as_built)

    def test_nonreviewer_prompts_and_standing_rules_do_not_create_a_second_style_owner(self) -> None:
        write = (REPO / "docs/direct/write_prompt_contract.md").read_text()
        standing = (REPO / "config/review/code.md").read_text()
        checks = (REPO / "docs/direct/phase_checks.md").read_text()
        director = (REPO / "commands/unit/direct.md").read_text()
        closing = (REPO / "docs/direct/final_gate_commit.md").read_text()

        self.assertIn("Implementation, fix and test prompts carry verbatim:", write)
        self.assertIn('"Do not open the `rust_style` skill or run `load-rust-style.sh`;', write)
        self.assertIn("Review prompts use\n   <ReviewPromptContract/>'s style assignment instead.", write)
        self.assertIn("read-only reviewer assigned by <ReviewPromptContract/>", checks)
        self.assertIn("including a reviewer's style finding", director)
        self.assertNotIn("verification, smoke, style", director)
        self.assertNotIn("style review and closing repairs", closing)
        self.assertIn("commands/unit/style_review.md", standing)
        self.assertNotIn("Read each applicable rule", standing)
        self.assertNotIn("~/rust/nate_style/rust/", standing)


if __name__ == "__main__":
    _ = unittest.main()
