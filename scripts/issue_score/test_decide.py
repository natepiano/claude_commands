"""Decision rules from the Design, including zero-based API Score levels."""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import cast

from scripts.issue_score import client, decide

FIXTURES = Path(__file__).with_name("fixtures")


def score_answer(probabilities: dict[str, float]) -> client.ScoreAnswer:
    return {
        "type": "score", "score": sum(int(level) * p for level, p in probabilities.items()),
        "confidence": max(probabilities.values()),
        "legend": {level: f"Situation {level}" for level in probabilities},
        "probabilities": probabilities,
    }


def choice_answer(probabilities: dict[str, float]) -> client.ChoiceAnswer:
    choice = max(probabilities, key=lambda name: probabilities[name])
    return {"type": "choice", "choice": choice, "confidence": probabilities[choice],
            "probabilities": probabilities}


class DecideTests(unittest.TestCase):
    def test_median_uses_cumulative_mass_in_numeric_order_not_mean_or_mode(self) -> None:
        # Mean rounds to 2 and the mode is 0; the median API level is 1.
        probabilities = {"4": 0.3, "0": 0.4, "3": 0.0, "1": 0.2, "2": 0.1}
        self.assertEqual(decide.median_level(probabilities), 1)

    def test_split_requires_non_neighbors_and_includes_quarter_mass(self) -> None:
        cases = (
            ({"0": 0.25, "1": 0.5, "2": 0.25}, True),
            ({"0": 0.25, "1": 0.75, "2": 0.0}, False),
            ({"0": 0.24, "1": 0.52, "2": 0.24}, False),
        )
        for probabilities, expected in cases:
            with self.subTest(probabilities=probabilities):
                self.assertEqual(decide.is_split(probabilities), expected)

    def test_star_decision_converts_zero_based_median_and_uses_its_probability(self) -> None:
        answer = score_answer({"0": .05, "1": .1, "2": .6, "3": .2, "4": .05})
        decision = decide.decide_score(answer, "backlog_impact")
        self.assertEqual(decision.value, 3)
        self.assertEqual(decision.certainty, "sure")
        self.assertAlmostEqual(decision.confidence, .6)

    def test_a_split_answer_is_unsure_even_when_proposed_level_meets_threshold(self) -> None:
        decision = decide.decide_score(
            score_answer({"0": .25, "1": .0, "2": .65, "3": .1, "4": .0}), "backlog_impact",
        )
        self.assertEqual(decision.value, 3)
        self.assertTrue(decision.split)
        self.assertEqual(decision.certainty, "unsure")

    def test_effort_remains_marked_for_human_check(self) -> None:
        response = cast(client.Response, json.loads((FIXTURES / "main_response.json").read_text()))
        answer = cast(client.ScoreAnswer, response["answers"]["effort"])
        decision = decide.decide_score(answer, "backlog_effort")
        self.assertEqual(decision.value, 2)
        self.assertTrue(decision.check)

    def test_choice_averages_distributions_by_name_and_flags_order_disagreement(self) -> None:
        forward = choice_answer({"feature": .8, "enhancement": .2})
        reverse = choice_answer({"enhancement": .7, "feature": .3})
        decision = decide.decide_choice(forward, reverse, "category")
        self.assertEqual(decision.value, "feature")
        self.assertAlmostEqual(decision.confidence, .55)
        self.assertEqual(decision.certainty, "unsure")

    def test_disagreeing_orders_stay_unsure_even_if_average_exceeds_threshold(self) -> None:
        decision = decide.decide_choice(
            choice_answer({"feature": .99, "refactor": .01}),
            choice_answer({"refactor": .51, "feature": .49}), "category",
        )
        self.assertEqual(decision.value, "feature")
        self.assertAlmostEqual(decision.confidence, .74)
        self.assertEqual(decision.certainty, "unsure")

    def test_urgency_pressure_rows_without_evidence_propose_one_to_three(self) -> None:
        evidence: dict[str, float] = {name: .05 for name in (
            "deadline_named", "outside_cutoff", "current_harm", "blocks_planned_work",
        )}
        for level, stars in ((0, 1), (1, 2), (2, 3)):
            with self.subTest(level=level):
                decision = decide.decide_urgency(evidence, score_answer({str(level): 1.0}))
                self.assertEqual(decision.value, stars)
                self.assertFalse(decision.possible_five)
                self.assertEqual(decision.certainty, "sure")

    def test_each_strong_evidence_row_proposes_four_and_flags_possible_five(self) -> None:
        names = ("deadline_named", "outside_cutoff", "current_harm", "blocks_planned_work")
        for name in names:
            for level in (0, 1, 2):
                with self.subTest(evidence=name, pressure=level):
                    evidence: dict[str, float] = dict.fromkeys(names, .05)
                    evidence[name] = .95
                    decision = decide.decide_urgency(evidence, score_answer({str(level): 1.0}))
                    self.assertEqual(decision.value, 4)
                    self.assertTrue(decision.possible_five)

    def test_uncertain_evidence_and_exact_cutoff_do_not_promote_to_four(self) -> None:
        for probability in (.3, .5, .7):
            with self.subTest(probability=probability):
                decision = decide.decide_urgency(
                    {"deadline_named": probability, "outside_cutoff": .05,
                     "current_harm": .05, "blocks_planned_work": .05},
                    score_answer({"0": .8, "1": .15, "2": .05}),
                )
                self.assertEqual(decision.value, 1)
                self.assertEqual(decision.certainty, "unsure")

    def test_urgency_never_returns_five_even_when_every_signal_is_certain(self) -> None:
        decision = decide.decide_urgency(
            {"deadline_named": 1.0, "outside_cutoff": 1.0,
             "current_harm": 1.0, "blocks_planned_work": 1.0},
            score_answer({"0": 0.0, "1": 0.0, "2": 1.0}),
        )
        self.assertEqual(decision.value, 4)
        self.assertTrue(decision.possible_five)

    def test_area_below_fit_cutoff_is_unfiled_with_top_area_as_hint(self) -> None:
        main = cast(client.Response, json.loads((FIXTURES / "main_response.json").read_text()))
        area = cast(client.Response, json.loads((FIXTURES / "area_response.json").read_text()))
        area["answers"]["area_fits"] = {"type": "noul", "noul": .29}
        decision = decide.decide_answers(main["answers"], area["answers"])["area"]
        self.assertEqual(decision.value, "unfiled")
        self.assertEqual(decision.hint, "rendering")

    def test_area_at_fit_cutoff_keeps_top_area(self) -> None:
        main = cast(client.Response, json.loads((FIXTURES / "main_response.json").read_text()))
        area = cast(client.Response, json.loads((FIXTURES / "area_response.json").read_text()))
        area["answers"]["area_fits"] = {"type": "noul", "noul": .3}
        decision = decide.decide_answers(main["answers"], area["answers"])["area"]
        self.assertEqual(decision.value, "rendering")

    def test_absent_area_is_sure_even_when_hint_choices_disagree(self) -> None:
        main = cast(client.Response, json.loads((FIXTURES / "main_response.json").read_text()))
        area = cast(client.Response, json.loads((FIXTURES / "area_response.json").read_text()))
        area["answers"]["area_fits"] = {"type": "noul", "noul": .1}
        area["answers"]["area"] = choice_answer({"rendering": .9, "tooling": .1})
        area["answers"]["area_reverse"] = choice_answer({"tooling": .6, "rendering": .4})
        decision = decide.decide_answers(main["answers"], area["answers"])["area"]
        self.assertEqual(decision.value, "unfiled")
        self.assertEqual(decision.certainty, "sure")
        self.assertAlmostEqual(decision.confidence, .9)
        self.assertEqual(decision.hint, "rendering")

    def test_ambiguous_area_fit_and_disagreeing_orders_require_review(self) -> None:
        for fit, reverse in ((.5, {"tooling": .1, "rendering": .9}),
                             (.95, {"tooling": .51, "rendering": .49})):
            with self.subTest(fit=fit, reverse=reverse):
                main = cast(client.Response, json.loads((FIXTURES / "main_response.json").read_text()))
                area = cast(client.Response, json.loads((FIXTURES / "area_response.json").read_text()))
                area["answers"]["area_fits"] = {"type": "noul", "noul": fit}
                area["answers"]["area_reverse"] = choice_answer(reverse)
                decision = decide.decide_answers(main["answers"], area["answers"])["area"]
                self.assertEqual(decision.value, "rendering")
                self.assertEqual(decision.certainty, "unsure")


if __name__ == "__main__":
    _ = unittest.main()
