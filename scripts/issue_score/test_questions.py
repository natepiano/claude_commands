"""Question wording identifies the state fields the model must read."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from scripts.issue_score import experiment, questions, vault

ADOPTED_WORDINGS = Path(__file__).with_name("fixtures") / "adopted_wordings_v3.json"


class QuestionTests(unittest.TestCase):
    def test_adopted_area_definitions_fill_both_orders_without_changing_the_option_domain(self) -> None:
        areas = ["rendering", "tooling"]
        definitions = {"rendering": "Drawing scenes and managing render passes.",
                       "not-in-vocabulary": "Must not add an option."}
        original = questions.area_questions(areas, {})
        defined = questions.area_questions(areas, definitions)
        for name, order in (("area", areas), ("area_reverse", list(reversed(areas)))):
            with self.subTest(question=name):
                question = defined[name]
                fallback = original[name]
                assert question["type"] == "choice" and fallback["type"] == "choice"
                self.assertEqual(list(question["criteria"]), order)
                self.assertEqual(question["criteria"], {
                    "rendering": "The work concerns rendering: Drawing scenes and managing render passes.",
                    "tooling": "The work concerns the part named tooling.",
                })
                self.assertEqual(fallback["criteria"]["rendering"], "The work concerns the part named rendering.")
                self.assertEqual(question["instructions"], fallback["instructions"])
        self.assertEqual(defined["area_fits"], original["area_fits"])

    def test_merged_area_definitions_invalidate_earlier_question_versions(self) -> None:
        self.assertEqual(questions.QUESTION_SET_VERSION, "5")

    def test_jev_proposes_exactly_the_fields_approved_by_the_user(self) -> None:
        self.assertEqual(
            questions.JEV_FIELDS,
            {"backlog_goal", "backlog_impact", "backlog_alignment", "area"},
        )

    def test_every_jev_proposed_field_has_a_supported_threshold_policy(self) -> None:
        for field in questions.JEV_FIELDS:
            with self.subTest(field=field):
                self.assertIn(field, questions.THRESHOLDS)
                self.assertIsInstance(questions.THRESHOLDS[field], (questions.Supported, questions.MinimumGap))

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
                        if issue.path.stem in questions.HISTORICAL_EFFORT_EXAMPLE_STEMS}
            self.assertEqual(excluded, examples)
            self.assertEqual(questions.HISTORICAL_EFFORT_EXAMPLE_STEMS - {issue.path.stem for issue in loaded}, set())

    def test_instructions_identify_the_issue_state_path_in_both_requests(self) -> None:
        for request in (questions.main_questions(), questions.area_questions(["rendering", "tooling"], {})):
            for name, question in request.items():
                with self.subTest(question=name):
                    self.assertIn("`issue`", question["instructions"])
                    self.assertNotRegex(question["instructions"], r"(?<!`)\bissue\b(?!`)")

    def test_criteria_quote_issue_paths_and_area_questions_identify_the_vocabulary(self) -> None:
        area = questions.area_questions(["rendering", "tooling"], {})
        adopted = experiment.load_variants(ADOPTED_WORDINGS)["category_deliverable"]
        for request in (questions.main_questions(), area):
            for name, question in request.items():
                criteria = question["criteria"]
                descriptions = criteria.values() if isinstance(criteria, dict) else criteria
                for description in descriptions:
                    with self.subTest(question=name, criterion=description):
                        if name in adopted:
                            approved = adopted[name]
                            assert approved["type"] == "choice"
                            # Measured criteria refer to the issue in prose verbatim.
                            # Only an exact approved description gets this exception.
                            if description in approved["criteria"].values():
                                continue
                        self.assertNotRegex(description, r"(?<!`)\bissue\b(?!`)")
        for name, question in area.items():
            with self.subTest(question=name):
                self.assertIn("`areas`", question["instructions"])
                self.assertNotRegex(question["instructions"], r"\bin areas\b")

    def test_adopted_harm_and_deliverable_wordings_are_the_production_questions(self) -> None:
        # Snapshot of the selected variants, independent of production builders.
        adopted = experiment.load_variants(ADOPTED_WORDINGS)
        production = questions.main_questions()
        for replacements in adopted.values():
            for name, expected in replacements.items():
                with self.subTest(question=name):
                    actual = production[name]
                    self.assertEqual(actual, expected)
                    assert actual["type"] != "score" and expected["type"] != "score"
                    self.assertEqual(list(actual["criteria"].items()), list(expected["criteria"].items()))

    def test_effort_request_uses_the_selected_closed_issue_anchors(self) -> None:
        effort = questions.main_questions()["effort"]
        assert effort["type"] == "score"
        anchors = (
            ("sccache gap start and length in one type", "cpu usage directly in the cpu pane"),
            ("phase state lifecycle enum", "cmd-d duplicate for movables"),
            ("asset loading", "arrange jacks widget"),
            ("feature log", "panel picking design proposal"),
            ("headless widgets v1", "tool graph v1"),
        )
        self.assertEqual(len(effort["criteria"]), len(anchors))
        for level, (description, examples) in enumerate(zip(effort["criteria"], anchors), 1):
            with self.subTest(level=level):
                for example in examples:
                    self.assertIn(example, description.casefold())


if __name__ == "__main__":
    _ = unittest.main()
