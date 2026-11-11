"""The unit command and the hooks that re-read it."""

from __future__ import annotations

import io
import json
import runpy
import unittest
from collections.abc import Callable
from contextlib import redirect_stdout
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

from scripts.production import staff
from scripts.production.staff_test_support import StaffTestCase, fixture_member, fixture_runtime_id
from scripts.hooks import direct_run


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


class UnitCompactionTests(StaffTestCase):
    def test_compaction_loads_the_recorded_unit_command(self) -> None:
        runner = fixture_member("showrunner", name="demo")
        for unit_type in staff.UNIT_TYPES:
            with self.subTest(unit_type=unit_type):
                row = staff.ensure("unit", name=unit_type, parent=staff.ReportsTo(runner.id),
                    runtime_id=fixture_runtime_id(unit_type), unit_type=unit_type)
                payload = {"source": "compact", "session_id": row.current_session}
                output = io.StringIO()
                with (patch.object(direct_run, "active_run", return_value=self.root / "run"),
                      patch("sys.stdin", io.StringIO(json.dumps(payload))), redirect_stdout(output)):
                    module = runpy.run_path(str(REPO / "scripts/hooks/session-start-direct-resume.py"))
                    main = cast(Callable[[], None], module["main"])
                    main()
                response = cast(dict[str, object], json.loads(output.getvalue()))
                details = cast(dict[str, object], response["hookSpecificOutput"])
                context = details["additionalContext"]
                assert isinstance(context, str)
                self.assertIn(f"/unit:{unit_type}", context)
                self.assertIn(f"~/.claude/commands/unit/{unit_type}.md", context)
                if unit_type != "direct":
                    self.assertNotIn("commands/unit/direct.md", context)


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
        director = (REPO / "docs/direct/repair_rounds.md").read_text()
        core = (REPO / "commands/unit/direct.md").read_text()
        closing = (REPO / "docs/direct/final_gate_commit.md").read_text()

        self.assertIn("Implementation, fix and test prompts carry verbatim:", write)
        self.assertIn('"Do not open the `rust_style` skill or run `load-rust-style.sh`;', write)
        self.assertIn("Review prompts use\n   <ReviewPromptContract/>'s style assignment instead.", write)
        self.assertIn("read-only reviewer assigned by <ReviewPromptContract/>", checks)
        self.assertIn("including a reviewer's style finding", director)
        self.assertNotIn("verification, smoke, style", director)
        self.assertNotIn("verification, smoke, style", core)
        self.assertNotIn("style review and closing repairs", closing)
        self.assertIn("commands/unit/style_review.md", standing)
        self.assertNotIn("Read each applicable rule", standing)
        self.assertNotIn("~/rust/nate_style/rust/", standing)


if __name__ == "__main__":
    _ = unittest.main()
