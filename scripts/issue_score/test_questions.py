"""Question wording identifies the state fields the model must read."""
from __future__ import annotations

import unittest

from scripts.issue_score import questions


class QuestionTests(unittest.TestCase):
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
