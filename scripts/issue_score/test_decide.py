"""Decision rules from the Design, including zero-based API Score levels."""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Literal, cast
from unittest import mock

from scripts.issue_score import client, decide, questions

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
    def test_configured_tuning_policy_controls_certainty_at_the_measured_boundaries(self) -> None:
        # Phase 4 adds the adopted defined-area threshold to the tuned policy.
        expected = {"backlog_goal": .4, "category": .9,
                    "backlog_impact": .4, "backlog_alignment": .4, "area": .9}
        for field, threshold in expected.items():
            with self.subTest(field=field):
                self.assertEqual(questions.THRESHOLDS[field], decide.Supported(threshold))
                below = decide.FieldDecision("fixture", threshold - .001, "unsure")
                at = decide.FieldDecision("fixture", threshold, "unsure")
                self.assertFalse(decide.is_sure(field, below))
                self.assertTrue(decide.is_sure(field, at))
        for field in ("backlog_urgency", "backlog_effort"):
            self.assertIsInstance(questions.THRESHOLDS[field], decide.ReportOnly)

    def test_unsupported_area_never_becomes_sure_even_for_certain_choices_or_fallback(self) -> None:
        main = cast(client.Response, json.loads((FIXTURES / "main_response.json").read_text()))
        area = cast(client.Response, json.loads((FIXTURES / "area_response.json").read_text()))
        origins: tuple[Literal["choice", "area_fallback"], ...] = ("choice", "area_fallback")
        thresholds = dict(questions.THRESHOLDS, area=decide.Unsupported())
        for confidence in (0., .4, .95, 1.):
            for origin in origins:
                with self.subTest(confidence=confidence, origin=origin):
                    decision = decide.FieldDecision("unfiled", confidence, "unsure", origin=origin)
                    self.assertFalse(decide.is_sure("area", decision, thresholds))
        for fits in (0., 1.):
            area["answers"]["area_fits"] = {"type": "noul", "noul": fits}
            for name in ("area", "area_reverse"):
                area["answers"][name] = choice_answer({"rendering": 1., "tooling": 0.})
            with mock.patch.dict(questions.THRESHOLDS, area=decide.Unsupported()):
                result = decide.decide_answers(main["answers"], area["answers"])["area"]
            self.assertEqual(result.certainty, "unsure")
            self.assertEqual(result.confidence, 1.)

    def test_report_only_urgency_and_effort_keep_values_for_review_without_becoming_sure(self) -> None:
        effort = decide.decide_score(score_answer({"0": 0., "1": 1.}), "backlog_effort")
        urgency = decide.decide_urgency(
            dict.fromkeys(("deadline_named", "outside_cutoff", "current_harm", "blocks_planned_work"), 0.),
            score_answer({"0": 0., "1": 1., "2": 0.}),
        )
        for field, decision in (("backlog_effort", effort), ("backlog_urgency", urgency)):
            with self.subTest(field=field):
                self.assertEqual(decision.value, 2)
                self.assertEqual(decision.confidence, 1.)
                self.assertEqual(decision.certainty, "unsure")
                self.assertFalse(decide.is_sure(field, decision))
        self.assertTrue(effort.check)

    def test_alignment_dependency_uses_the_adopted_goal_threshold(self) -> None:
        main = cast(client.Response, json.loads((FIXTURES / "main_response.json").read_text()))
        area = cast(client.Response, json.loads((FIXTURES / "area_response.json").read_text()))
        main["answers"]["alignment_ship_hana"] = score_answer({"0": 0., "1": 0., "2": .05, "3": .95, "4": 0.})
        for confidence, expected in ((.399, False), (.4, True)):
            with self.subTest(goal_confidence=confidence):
                for name in ("goal", "goal_reverse"):
                    main["answers"][name] = choice_answer({
                        "ship_hana": confidence, "find_collaborators": .3,
                        "seek_investors": .2, "business_viability": .5 - confidence,
                    })
                result = decide.decide_answers(main["answers"], area["answers"])["backlog_alignment"]
                self.assertEqual(decide.is_sure("backlog_alignment", result), expected)
                self.assertEqual(result.certainty, "sure" if expected else "unsure")

    def test_alignment_cannot_be_sure_when_its_certain_goal_dependency_has_no_supported_threshold(self) -> None:
        decision = decide.FieldDecision(4, 1., "unsure", threshold_dependencies={"backlog_goal": 1.})
        for policy in (decide.Unsupported(), decide.ReportOnly()):
            with self.subTest(goal_policy=policy):
                thresholds = dict(questions.THRESHOLDS)
                thresholds["backlog_goal"] = policy
                self.assertFalse(decide.is_sure("backlog_alignment", decision, thresholds))

    def test_threshold_selection_chooses_lowest_qualifying_positive_coverage(self) -> None:
        points = [decide.RiskPoint(.85, .1, .0), decide.RiskPoint(.4, .9, .11),
                  decide.RiskPoint(.55, .7, .1), decide.RiskPoint(.45, 0., .0)]
        for field in ("backlog_goal", "category", "area", "backlog_alignment", "backlog_impact"):
            with self.subTest(field=field):
                result = decide.select_threshold(field, points)
                self.assertIsInstance(result, decide.Supported)
                if isinstance(result, decide.Supported):
                    self.assertEqual(result.threshold, .55)

    def test_threshold_selection_names_failure_and_report_only_outcomes(self) -> None:
        for points in ([decide.RiskPoint(.4, 1., .1001), decide.RiskPoint(.95, .1, .2)],
                       [decide.RiskPoint(.4, 0., .0)], []):
            with self.subTest(points=points):
                result = decide.select_threshold("category", points)
                self.assertIsInstance(result, decide.Unsupported)
                if isinstance(result, decide.Unsupported):
                    self.assertTrue(result.reason)
        for field in ("backlog_urgency", "backlog_effort"):
            self.assertIsInstance(decide.select_threshold(field, [decide.RiskPoint(.4, 1., 0.)]), decide.ReportOnly)
        self.assertIsInstance(decide.select_threshold("area", [decide.RiskPoint(.4, 1., 0.)], complete=False), decide.Unsupported)

    def test_split_and_order_disagreement_stay_unsure_at_every_swept_threshold(self) -> None:
        split = decide.decide_score(score_answer({"0": .25, "1": 0., "2": .75}), "backlog_impact")
        disagreement = decide.decide_choice(choice_answer({"feature": .99, "refactor": .01}),
                                            choice_answer({"feature": .49, "refactor": .51}), "category")
        for threshold in (percent / 100 for percent in range(40, 100, 5)):
            thresholds = dict(questions.THRESHOLDS)
            for field, decision in (("backlog_impact", split), ("category", disagreement)):
                thresholds[field] = decide.Supported(threshold)
                with self.subTest(threshold=threshold, field=field):
                    self.assertFalse(decide.is_sure(field, decision, thresholds))

    def test_alignment_uses_goal_dependency_at_its_own_threshold(self) -> None:
        main = cast(client.Response, json.loads((FIXTURES / "main_response.json").read_text()))
        area = cast(client.Response, json.loads((FIXTURES / "area_response.json").read_text()))
        main["answers"]["goal"] = choice_answer({"ship_hana": .65, "find_collaborators": .35})
        main["answers"]["goal_reverse"] = choice_answer({"ship_hana": .65, "find_collaborators": .35})
        main["answers"]["alignment_ship_hana"] = score_answer({"0": 0., "1": 0., "2": .2, "3": .8, "4": 0.})
        decision = decide.decide_answers(main["answers"], area["answers"])["backlog_alignment"]
        thresholds = dict(questions.THRESHOLDS)
        thresholds.update(backlog_goal=decide.Supported(.6), backlog_alignment=decide.Supported(.75))
        self.assertTrue(decide.is_sure("backlog_alignment", decision, thresholds))
        thresholds["backlog_goal"] = decide.Supported(.7)
        self.assertFalse(decide.is_sure("backlog_alignment", decision, thresholds))

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
                self.assertEqual(decision.certainty, "unsure")

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

    def test_unfiled_choice_and_fallback_have_distinct_origins_and_sure_guards(self) -> None:
        main = cast(client.Response, json.loads((FIXTURES / "main_response.json").read_text()))
        area = cast(client.Response, json.loads((FIXTURES / "area_response.json").read_text()))
        area["answers"]["area"] = choice_answer({"unfiled": .99, "rendering": .01})
        area["answers"]["area_reverse"] = choice_answer({"unfiled": .49, "rendering": .51})
        for fits, origin, hint in ((.95, "choice", ""), (.1, "area_fallback", "unfiled")):
            with self.subTest(fits=fits):
                area["answers"]["area_fits"] = {"type": "noul", "noul": fits}
                decision = decide.decide_answers(main["answers"], area["answers"])["area"]
                self.assertEqual(decision.value, "unfiled")
                self.assertEqual(decision.origin, origin)
                self.assertEqual(decision.hint, hint)
                self.assertEqual(decide.is_sure("area", decision), origin == "area_fallback")

    def test_area_at_fit_cutoff_keeps_top_area(self) -> None:
        main = cast(client.Response, json.loads((FIXTURES / "main_response.json").read_text()))
        area = cast(client.Response, json.loads((FIXTURES / "area_response.json").read_text()))
        area["answers"]["area_fits"] = {"type": "noul", "noul": .3}
        decision = decide.decide_answers(main["answers"], area["answers"])["area"]
        self.assertEqual(decision.value, "rendering")

    def test_absent_area_is_sure_at_the_adopted_threshold_despite_disagreeing_hint_orders(self) -> None:
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
