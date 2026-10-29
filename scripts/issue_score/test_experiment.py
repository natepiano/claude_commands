"""Wording variants preserve production questions and their decision semantics."""
from __future__ import annotations

import copy
import gzip
import json
from pathlib import Path
from typing import cast, override
import unittest
from unittest import mock
from urllib.request import Request

from scripts.issue_score import client, decide, evaluate, experiment, questions, records, score_issues
from scripts.issue_score import test_score_issues as run_support
from scripts.issue_score import test_evaluate as report_support
from scripts.issue_score.test_decide import choice_answer, score_answer

FIXTURES = Path(__file__).with_name("fixtures")


class ExperimentTests(unittest.TestCase):
    def test_variants_are_added_without_replacing_or_mutating_production_questions(self) -> None:
        production = questions.main_questions()
        before = copy.deepcopy(production)
        replacement = copy.deepcopy(production["deadline_named"])
        replacement["instructions"] = "Does `issue` explicitly require completion before an event?"
        variants = {"explicit_deadline": {"deadline_named": replacement}}
        expanded = experiment.build_questions(production, variants)
        self.assertEqual(production, before)
        self.assertEqual(set(expanded), set(production) | {"deadline_named__explicit_deadline"})
        for name, question in production.items():
            self.assertEqual(expanded[name], question)
        self.assertEqual(expanded["deadline_named__explicit_deadline"], replacement)

    def test_variant_answers_are_judged_as_production_names_with_unchanged_answers_preserved(self) -> None:
        production = questions.main_questions()
        variants = {
            "deadline_scope": {"deadline_named": copy.deepcopy(production["deadline_named"])},
            "benefit_scope": {"impact": copy.deepcopy(production["impact"])},
        }
        expanded = experiment.build_questions(production, variants)
        raw = cast(client.Response, json.loads((FIXTURES / "main_response.json").read_text()))
        raw["answers"]["deadline_named__deadline_scope"] = {"type": "noul", "noul": .95}
        raw["answers"]["impact__benefit_scope"] = score_answer({"0": 0., "1": .1, "2": 0., "3": 0., "4": .9})
        response = client.parse_response(raw, expanded)
        unchanged = copy.deepcopy(response)
        area = client.parse_response(cast(object, json.loads((FIXTURES / "area_response.json").read_text())),
                                     questions.area_questions(["rendering", "tooling"]))
        for variant, replaced in (("production", ""), ("deadline_scope", "deadline_named"), ("benefit_scope", "impact")):
            with self.subTest(variant=variant):
                projected = experiment.project_response(response, variant, variants)
                expected: client.Response = {
                    "model": response["model"], "usage": response["usage"],
                    "answers": {name: response["answers"][name] for name in production},
                }
                if replaced:
                    expected["answers"][replaced] = response["answers"][f"{replaced}__{variant}"]
                self.assertEqual(set(projected["answers"]), set(production))
                self.assertEqual(projected, expected)
                decisions = decide.decide_answers(projected["answers"], area["answers"])
                self.assertEqual(decisions, decide.decide_answers(expected["answers"], area["answers"]))
                self.assertEqual(decisions["backlog_urgency"].value, 4 if variant == "deadline_scope" else 1)
                self.assertEqual(decisions["backlog_impact"].value, 5 if variant == "benefit_scope" else 3)
        self.assertEqual(response, unchanged)

    def test_area_choice_variants_project_only_the_area_request_and_preserve_fit(self) -> None:
        production = questions.area_questions(["rendering", "tooling"])
        variants = {"area_scope": {name: copy.deepcopy(production[name]) for name in ("area", "area_reverse")},
                    "deadline_scope": {"deadline_named": questions.main_questions()["deadline_named"]}}
        expanded = experiment.build_questions(production, variants)
        self.assertEqual(set(expanded), set(production) | {"area__area_scope", "area_reverse__area_scope"})
        raw = cast(client.Response, json.loads((FIXTURES / "area_response.json").read_text()))
        for name in ("area", "area_reverse"):
            raw["answers"][name + "__area_scope"] = choice_answer({"rendering": .1, "tooling": .9})
        response = client.parse_response(raw, expanded)
        main = client.parse_response(cast(object, json.loads((FIXTURES / "main_response.json").read_text())),
                                     questions.main_questions())
        projected = experiment.project_response(response, "area_scope", variants)
        self.assertEqual(set(projected["answers"]), set(production))
        self.assertEqual(projected["answers"]["area_fits"], response["answers"]["area_fits"])
        self.assertEqual(decide.decide_answers(main["answers"], projected["answers"])["area"].value, "tooling")
        original = experiment.project_response(response, "production", variants)
        self.assertEqual(decide.decide_answers(main["answers"], original["answers"])["area"].value, "rendering")

    def test_missing_variant_answer_is_an_error_instead_of_a_production_fallback(self) -> None:
        production = questions.main_questions()
        variants = {"deadline_scope": {"deadline_named": production["deadline_named"]}}
        response = client.parse_response(cast(object, json.loads((FIXTURES / "main_response.json").read_text())), production)
        with self.assertRaises(client.TypeSafeError):
            _ = experiment.project_response(response, "deadline_scope", variants)

    def test_wording_variants_cannot_change_the_decision_option_domain(self) -> None:
        production = questions.main_questions()
        changed = copy.deepcopy(production["category"])
        assert changed["type"] == "choice"
        changed["criteria"]["new_category"] = "A new answer that production cannot interpret."
        with self.assertRaises(ValueError):
            _ = experiment.build_questions(production, {"incompatible": {"category": changed}})


class ExperimentRunTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.fixture: run_support.ScoreIssuesTests = run_support.ScoreIssuesTests()

    @override
    def setUp(self) -> None:
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        _ = self.enterContext(mock.patch.object(client, "SPEND_LEDGER", self.fixture.state / "spend.jsonl"))

    def test_experiment_is_fresh_tune_only_and_persists_projected_variants_without_poisoning_cache(self) -> None:
        tune = self.fixture.issue("Target A")
        holdout_title = next(f"Target 00 {number}" for number in range(50)
                             if evaluate.split_for(f"Target 00 {number}.md") == "holdout")
        _ = self.fixture.issue(holdout_title, area="tooling")
        with mock.patch.object(client, "urlopen", side_effect=self.fixture.replay):
            cached = self.fixture.records(self.fixture.score(only="Target A"))
        production = questions.main_questions()
        variants = {"alternate": {name: copy.deepcopy(production[name]) for name in ("impact", "deadline_named")}}
        payloads: list[records.RequestPayload] = []
        raw_answers: dict[str, client.Response] = {}

        def replay(request: Request, timeout: float = 60) -> run_support.FixtureHTTPResponse:
            _ = timeout
            payload = cast(records.RequestPayload, json.loads(gzip.decompress(cast(bytes, request.data))))
            payloads.append(payload)
            filename = "area_response.json" if "areas" in payload["state"] else "main_response.json"
            response = cast(client.Response, json.loads((FIXTURES / filename).read_text()))
            if filename == "main_response.json":
                response["answers"]["impact__alternate"] = score_answer({"0": 0., "1": 0., "2": 0., "3": 0., "4": 1.})
                response["answers"]["deadline_named__alternate"] = {"type": "noul", "noul": .95}
            raw_answers["area" if filename == "area_response.json" else "main"] = copy.deepcopy(response)
            return run_support.FixtureHTTPResponse(json.dumps(response).encode())

        with mock.patch.object(client, "urlopen", side_effect=replay) as http:
            runs = score_issues.experiment(variants, only="Target*", limit=1, issues_dir=self.fixture.issues, state_dir=self.fixture.state)
            repeat = score_issues.experiment(variants, only="Target*", limit=1, issues_dir=self.fixture.issues, state_dir=self.fixture.state)
        self.assertEqual(http.call_count, 4)
        self.assertEqual({payload["state"]["issue"]["title"] for payload in payloads}, {tune.stem})
        main_payload = next(payload for payload in payloads if "areas" not in payload["state"])
        area_payload = next(payload for payload in payloads if "areas" in payload["state"])
        self.assertEqual(main_payload["state"]["issue"], area_payload["state"]["issue"])
        for name, question in production.items():
            self.assertEqual(main_payload["questions"][name], question)
        self.assertEqual(set(runs), {"production", "alternate"})
        for variant, expected_impact, expected_urgency in (("production", 3, 1), ("alternate", 5, 4)):
            self.assertEqual(runs[variant].failure_count, 0)
            self.assertNotEqual(runs[variant].result_path, repeat[variant].result_path)
            entries = self.fixture.records(runs[variant].result_path)
            self.assertEqual(len(entries), 1)
            entry = entries[0]
            self.assertEqual(entry["issue_path"], str(tune))
            self.assertEqual(entry.get("variant_set"), variant)
            self.assertEqual(entry.get("experiment_answers"), raw_answers)
            self.assertEqual(entry["payloads"]["main"], main_payload)
            self.assertEqual(entry["payloads"]["area"], area_payload)
            self.assertEqual(set(entry["answers"]["main"]["answers"]), set(production))
            self.assertEqual(cast(dict[str, object], entry["decided_fields"]["backlog_impact"])["value"], expected_impact)
            self.assertEqual(cast(dict[str, object], entry["decided_fields"]["backlog_urgency"])["value"], expected_urgency)
        goals = self.fixture.root / "goals.md"
        _ = goals.write_text("## Current goals\n" + "".join(f"{number}. `{label}`\n" for number, label in enumerate(questions.GOAL_LABELS.values(), 1)))
        report_path = evaluate.compare(runs["alternate"].result_path.stem, split="tune",
                                       baseline_run_id=runs["production"].result_path.stem,
                                       issues_dir=self.fixture.issues, state_dir=self.fixture.state, goals_path=goals)
        impact = report_support.section(report_support.section(report_path.read_text(), "## Group: all (N=1)"), "#### backlog_impact")
        self.assertEqual(report_support.table(impact, "N")[0]["RPS"], "1.0000")
        with mock.patch.object(client, "urlopen", side_effect=AssertionError("Only the original production cache may be reused")):
            current = self.fixture.records(self.fixture.score(only="Target A"))
        self.assertEqual(current, cached)


if __name__ == "__main__":
    _ = unittest.main()
