"""Question wording identifies the state fields the model must read."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from scripts.issue_score import questions, vault


class QuestionTests(unittest.TestCase):
    def test_effort_example_stems_resolve_only_the_exact_open_example_files(self) -> None:
        examples = {
            "board prints pointer to its own json",
            "anchor refresh wakes only for its panels",
            "0.20.0 - handle enum variant-index reflect path (bevy PR 23137)",
            "NonZeroSize",
            "add gamepad camera modes to switcher",
            "audit bevy migration impact",
            "color picker widget",
            "cable auto routing",
            "implement network aware undo",
            "ai graph builder with playback",
        }
        with tempfile.TemporaryDirectory() as directory:
            issues = Path(directory)
            for stem in examples | {"Fix gamepad camera modes crash", "NonZeroSize regression"}:
                _ = (issues / f"{stem}.md").write_text(
                    '---\nstatus: open\nproject: hana\ntitle: Unrelated display title\n---\nFixture body.\n')
            loaded = vault.load_open_issues(issues)
            excluded = {issue.path.stem for issue in loaded
                        if issue.path.stem in questions.EFFORT_EXAMPLE_STEMS}
            self.assertEqual(excluded, examples)
            self.assertEqual(questions.EFFORT_EXAMPLE_STEMS - {issue.path.stem for issue in loaded}, set())

    def test_instructions_identify_the_issue_state_path_in_both_requests(self) -> None:
        for request in (questions.main_questions(), questions.area_questions(["rendering", "tooling"])):
            for name, question in request.items():
                with self.subTest(question=name):
                    self.assertIn("`issue`", question["instructions"])
                    self.assertNotRegex(question["instructions"], r"(?<!`)\bissue\b(?!`)")

    def test_criteria_quote_issue_paths_and_area_questions_identify_the_vocabulary(self) -> None:
        area = questions.area_questions(["rendering", "tooling"])
        for request in (questions.main_questions(), area):
            for name, question in request.items():
                criteria = question["criteria"]
                descriptions = criteria.values() if isinstance(criteria, dict) else criteria
                for description in descriptions:
                    with self.subTest(question=name, criterion=description):
                        self.assertNotRegex(description, r"(?<!`)\bissue\b(?!`)")
        for name, question in area.items():
            with self.subTest(question=name):
                self.assertIn("`areas`", question["instructions"])
                self.assertNotRegex(question["instructions"], r"\bin areas\b")


if __name__ == "__main__":
    _ = unittest.main()
