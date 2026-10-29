"""Hand-computed evaluation cases; all labels and runs are local fixtures."""
from __future__ import annotations

import contextlib
import copy
from collections.abc import Sequence
from dataclasses import asdict
import io
import json
import os
from pathlib import Path
import tempfile
from typing import Literal, cast, override
import unittest
from unittest import mock

from scripts.issue_score import client, decide, evaluate, questions, records, score_issues, vault
from scripts.prioritize import renumber

FIXTURES = Path(__file__).with_name("fixtures")


def section(report: str, heading: str) -> str:
    """Select one Markdown section without depending on table spacing."""
    lines = report.splitlines()
    start = lines.index(heading) + 1
    depth = len(heading) - len(heading.lstrip("#"))
    end = next((index for index in range(start, len(lines))
                if lines[index].startswith("#")
                and len(lines[index]) - len(lines[index].lstrip("#")) <= depth), len(lines))
    return "\n".join(lines[start:end])


def table(report: str, first_header: str) -> list[dict[str, str]]:
    lines = report.splitlines()
    start = next(index for index, line in enumerate(lines)
                 if line.startswith("|") and line.split("|")[1].strip() == first_header)
    headers = [cell.strip() for cell in lines[start].strip("|").split("|")]
    rows: list[dict[str, str]] = []
    for line in lines[start + 2:]:
        if not line.startswith("|"):
            break
        rows.append(dict(zip(headers, (cell.strip() for cell in line.strip("|").split("|")))))
    return rows


class MetricTests(unittest.TestCase):
    def test_split_hashes_the_filename_including_extension_and_ignores_parent(self) -> None:
        # SHA1 residues modulo ten are 1, 8, 9, and 7 respectively.
        for filename, expected in (("Target A.md", "tune"), ("Target G.md", "holdout"),
                                   ("Target K.md", "holdout"), ("Target L.md", "holdout")):
            with self.subTest(filename=filename):
                self.assertEqual(evaluate.split_for(filename), expected)
                self.assertEqual(evaluate.split_for(Path("relocated/vault") / filename), expected)

    def test_quadratic_kappa_weights_two_adjacent_mistakes_by_the_marginals(self) -> None:
        # Observed squared distance is 2/5; independent marginals give 18/5.
        result = evaluate.quadratic_weighted_kappa([1, 2, 3, 4, 5], [1, 2, 2, 4, 4])
        assert isinstance(result, float)
        self.assertAlmostEqual(result, 8 / 9)

    def test_quadratic_kappa_reports_complete_reversal_as_negative_agreement(self) -> None:
        result = evaluate.quadratic_weighted_kappa([1, 1, 5, 5], [5, 5, 1, 1])
        self.assertIsInstance(result, float)
        self.assertEqual(result, -1.0)

    def test_ranked_probability_score_uses_ordered_cumulative_errors(self) -> None:
        # Cumulative errors: .1, .3, -.3, -.1; squared sum .2, normalized by 4.
        self.assertAlmostEqual(evaluate.ranked_probability_score([.1, .2, .4, .2, .1], 2), .05)
        self.assertEqual(evaluate.ranked_probability_score([0., 0., 0., 0., 1.], 0), 1.)

    def test_three_level_pressure_score_uses_its_own_scale(self) -> None:
        # (.2**2 + (.5 - 1)**2) / 2, with a zero-based middle-level label.
        self.assertAlmostEqual(evaluate.ranked_probability_score([.2, .3, .5], 1), .145)

    def test_brier_counts_mass_assigned_to_every_wrong_option(self) -> None:
        self.assertAlmostEqual(evaluate.brier_score({"feature": .8, "enhancement": .125, "refactor": .075}, "enhancement"), 1.41125)
        self.assertAlmostEqual(evaluate.brier_score({"rendering": .875, "tooling": .125}, "new area"), 1.78125)

    def test_spearman_uses_midranks_for_ties_in_both_samples(self) -> None:
        # Ranks [1,2.5,2.5,4] and [4,1.5,1.5,3] have covariance -1.5/4.
        result = evaluate.spearman([10., 20., 20., 40.], [30., 10., 10., 20.])
        assert isinstance(result, float)
        self.assertAlmostEqual(result, -1 / 3)

    def test_undefined_agreement_and_correlation_are_explicit_outcomes(self) -> None:
        for result in (evaluate.quadratic_weighted_kappa([], []),
                       evaluate.quadratic_weighted_kappa([3, 3], [3, 3]),
                       evaluate.spearman([], []),
                       evaluate.spearman([1., 1.], [2., 3.])):
            with self.subTest(result=result):
                self.assertIsInstance(result, evaluate.UndefinedMetric)
                if isinstance(result, evaluate.UndefinedMetric):
                    self.assertTrue(result.reason)


class CompareTests(unittest.TestCase):
    root: Path = Path()
    issues: Path = Path()
    state: Path = Path()
    goals: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.issues = self.root / "vault" / "issues"
        self.issues.mkdir(parents=True)
        self.state = self.root / "state"
        (self.state / "runs").mkdir(parents=True)
        self.goals = self.root / "goals.md"
        _ = self.goals.write_text("## Current goals\n" + "".join(
            f"{number}. `{label}`\n" for number, label in enumerate(questions.GOAL_LABELS.values(), 1)))
        _ = self.enterContext(mock.patch.object(Path, "home", return_value=self.root))
        _ = self.enterContext(mock.patch.object(client, "SPEND_LEDGER", self.state / "spend.jsonl"))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "fixture-key"})))
        _ = self.enterContext(mock.patch.object(client, "urlopen", side_effect=AssertionError("Live HTTP forbidden")))

    def issue(self, title: str, *, impact: int = 3, score: int = 25,
              body: str = "Repair [[graph|the graph]].", extra: str = "") -> Path:
        path = self.issues / f"{title}.md"
        _ = path.write_text(
            '---\nstatus: open\nproject: "[[hana]]"\n'
            + 'backlog_goal: "1 - Ship Hana"\nbacklog_alignment: ⭐⭐⭐⭐\n'
            + f'backlog_impact: {"⭐" * impact}\n'
            + 'backlog_urgency: ⭐\nbacklog_effort: ⭐⭐\n'
            + f'backlog_score: {score}\ncategory: feature\narea: "[[rendering]]"\n'
            + extra + '---\n' + body,
            encoding="utf-8")
        return path

    def record(self, path: Path) -> records.ScoredIssue:
        issue = next(issue for issue in vault.load_rated_issues(self.issues).issues if issue.path == path)
        state = vault.build_state(issue)
        area_state: questions.AreaState = {"issue": state["issue"], "areas": ["rendering", "tooling"]}
        main_questions, area_questions = questions.main_questions(), questions.area_questions(area_state["areas"])
        main = client.parse_response(cast(object, json.loads((FIXTURES / "main_response.json").read_text())), main_questions)
        area = client.parse_response(cast(object, json.loads((FIXTURES / "area_response.json").read_text())), area_questions)
        return {
            "issue_path": str(path), "content_hash": score_issues.content_hash(issue, area_state["areas"]),
            "question_set_version": questions.QUESTION_SET_VERSION, "model": questions.MODEL,
            "payloads": {"main": {"model": questions.MODEL, "state": state, "questions": main_questions},
                         "area": {"model": questions.MODEL, "state": area_state, "questions": area_questions}},
            "answers": {"main": main, "area": area},
            "decided_fields": {name: asdict(value) for name, value in decide.decide_answers(main["answers"], area["answers"]).items()},
        }

    def run_file(self, run_id: str, entries: Sequence[records.ScoredIssue | records.FailedIssue]) -> Path:
        path = self.state / "runs" / f"{run_id}.jsonl"
        _ = path.write_text("".join(json.dumps(record) + "\n" for record in entries), encoding="utf-8")
        return path

    def compare(self, run_id: str, *, repeat_run_id: str = "", baseline_run_id: str = "",
                split: Literal["tune", "holdout", "all"] = "all") -> str:
        path = evaluate.compare(run_id, split=split, issues_dir=self.issues, state_dir=self.state,
                                goals_path=self.goals, repeat_run_id=repeat_run_id, baseline_run_id=baseline_run_id)
        self.assertEqual(path, self.state / "reports" / f"{run_id}-{split}.md")
        return path.read_text(encoding="utf-8")

    def refresh_decisions(self, record: records.ScoredIssue) -> None:
        record["decided_fields"] = {name: asdict(value) for name, value in decide.decide_answers(
            record["answers"]["main"]["answers"], record["answers"]["area"]["answers"]).items()}

    def score_answer(self, record: records.ScoredIssue, name: str, probabilities: dict[str, float]) -> None:
        answer = record["answers"]["main"]["answers"][name]
        assert answer["type"] == "score"
        answer["probabilities"] = probabilities
        answer["score"] = sum(int(level) * probability for level, probability in probabilities.items())
        answer["confidence"] = max(probabilities.values())
        self.refresh_decisions(record)

    def test_alignment_risk_sweeps_only_its_threshold_and_matches_production(self) -> None:
        path = self.issue("Target A")
        record = self.record(path)
        answers = record["answers"]["main"]["answers"]
        for name in ("goal", "goal_reverse"):
            answers[name] = {"type": "choice", "choice": "ship_hana", "confidence": .65,
                             "probabilities": {"ship_hana": .65, "find_collaborators": .35}}
        self.score_answer(record, "alignment_ship_hana", {"0": 0., "1": 0., "2": .2, "3": .8, "4": 0.})
        _ = self.run_file("unequal-thresholds", [record])
        decision = decide.decide_answers(answers, record["answers"]["area"]["answers"])["backlog_alignment"]
        for goal_threshold in (.4, .6, .7):
            with self.subTest(goal_threshold=goal_threshold), mock.patch.dict(questions.THRESHOLDS, backlog_goal=decide.Supported(goal_threshold)):
                report = self.compare("unequal-thresholds", split="tune")
                rows = table(section(section(report, "## Risk-coverage"), "### backlog_alignment"), "Threshold")
                for row in rows:
                    thresholds = dict(questions.THRESHOLDS)
                    thresholds["backlog_alignment"] = decide.Supported(float(row["Threshold"]))
                    expected = int(decide.is_sure("backlog_alignment", decision, thresholds))
                    self.assertEqual(int(row["Sure N"]), expected, row)
                by_threshold = {row["Threshold"]: row["Sure N"] for row in rows}
                self.assertEqual(by_threshold["0.75"], "1" if goal_threshold <= .65 else "0")

    def test_baseline_and_candidate_use_intersection_and_read_current_labels_once(self) -> None:
        common = self.issue("Target A", impact=1)
        candidate_only = self.issue("Target B", impact=5)
        baseline_only = self.issue("Target C", impact=5)
        example = self.issue(sorted(questions.HISTORICAL_EFFORT_EXAMPLE_STEMS)[0], impact=5)
        baseline = self.record(common)
        candidate = copy.deepcopy(baseline)
        self.score_answer(candidate, "impact", {"0": 0., "1": 0., "2": 0., "3": 0., "4": 1.})
        _ = self.run_file("cohort-baseline", [baseline, self.record(baseline_only), self.record(example)])
        _ = self.run_file("cohort-candidate", [candidate, self.record(candidate_only), self.record(example)])
        _ = common.write_text(common.read_text().replace("backlog_impact: ⭐\n", "backlog_impact: ⭐⭐⭐⭐⭐\n"))
        with mock.patch.object(vault, "load_rated_issues", wraps=vault.load_rated_issues) as loader:
            report = self.compare("cohort-candidate", baseline_run_id="cohort-baseline")
        loader.assert_called_once()
        candidate_section = report.split("## Baseline: cohort-baseline")[0]
        baseline_section = report.split("## Baseline: cohort-baseline", 1)[1]
        for part, heading, exact, mae in ((candidate_section, "## Group: all (N=1)", "1.0000", "0.0000"),
                                          (baseline_section, "## Baseline group: all (N=1)", "0.0000", "2.0000")):
            group = section(part, heading)
            metrics = table(section(group, "#### backlog_impact"), "N")[0]
            self.assertEqual((metrics["N"], metrics["Exact"], metrics["MAE"]), ("1", exact, mae))
        self.assertNotIn("## Group: all (N=2)", report)
        self.assertIn("Excluded effort examples: 1", report)

    def test_threshold_outcomes_require_complete_tuning_runs_and_keep_effort_and_urgency_report_only(self) -> None:
        path = self.issue("Target A")
        _ = self.run_file("threshold-complete", [self.record(path)])
        cases: tuple[tuple[Literal["tune", "holdout", "all"], str], ...] = (
            ("tune", "supported("), ("all", "unsupported"))
        for split, outcome in cases:
            with self.subTest(split=split):
                risk = section(self.compare("threshold-complete", split=split), "## Risk-coverage")
                for field in ("backlog_goal", "backlog_alignment", "backlog_impact", "category", "area"):
                    self.assertIn("Threshold outcome: " + outcome, section(risk, "### " + field))
                for field in ("backlog_urgency", "backlog_effort"):
                    self.assertIn("Threshold outcome: report-only", section(risk, "### " + field))
        missing_title = next(f"Missing tune {number}" for number in range(50)
                             if evaluate.split_for(f"Missing tune {number}.md") == "tune")
        _ = self.issue(missing_title)
        partial = self.compare("threshold-complete", split="tune")
        self.assertIn("partial", partial.splitlines()[0])
        self.assertIn("Threshold outcome: unsupported", section(section(partial, "## Risk-coverage"), "### backlog_impact"))

    def test_heldout_validates_the_configured_threshold_without_selecting_and_marks_incomplete_results_partial(self) -> None:
        paths: list[Path] = []
        for number in range(100):
            title = f"Heldout validation {number:03}"
            if evaluate.split_for(title + ".md") == "holdout":
                paths.append(self.issue(title))
                if len(paths) == 11:
                    break
        entries = [self.record(path) for path in paths]
        for index, record in enumerate(entries):
            confidence = .72 if index == 10 else .74
            for name in ("goal", "goal_reverse"):
                record["answers"]["main"]["answers"][name] = {
                    "type": "choice", "choice": "ship_hana", "confidence": confidence,
                    "probabilities": {"ship_hana": confidence, "find_collaborators": 1 - confidence},
                }
            self.refresh_decisions(record)
        for index in (0, 10):
            _ = paths[index].write_text(paths[index].read_text().replace(
                'backlog_goal: "1 - Ship Hana"', 'backlog_goal: "2 - Find Collaborators"'))
        _ = self.run_file("heldout-validation", entries)
        # .73 is between sweep points. Ten answers qualify; the eleventh,
        # wrong answer is below this configured threshold and must be excluded.
        with mock.patch.dict(questions.THRESHOLDS, backlog_goal=decide.Supported(.73)), \
                mock.patch.object(decide, "select_threshold", side_effect=AssertionError("Held-out data must not select thresholds")):
            passed = self.compare("heldout-validation", split="holdout")
            goal = section(section(passed, "## Risk-coverage"), "### backlog_goal")
            self.assertIn("Held-out validation: pass at configured threshold 0.73", goal)
            self.assertIn("Sure N: 10", goal)
            self.assertIn("coverage: 0.9091", goal)
            self.assertIn("exact error: 0.1000", goal)
            self.assertNotIn("Threshold outcome:", passed)
            recommendations = table(section(passed, "## Per-field recommendations and score ≥ 20 results"), "Field")
            self.assertIn("Jev proposes", next(row["Recommendation"] for row in recommendations if row["Field"] == "backlog_goal"))
            for field in ("backlog_urgency", "backlog_effort"):
                self.assertIn("Held-out validation: report-only", section(section(passed, "## Risk-coverage"), "### " + field))

            _ = paths[1].write_text(paths[1].read_text().replace(
                'backlog_goal: "1 - Ship Hana"', 'backlog_goal: "2 - Find Collaborators"'))
            failed = self.compare("heldout-validation", split="holdout")
            goal = section(section(failed, "## Risk-coverage"), "### backlog_goal")
            self.assertIn("Held-out validation: fail at configured threshold 0.73", goal)
            self.assertIn("exact error: 0.2000", goal)
            self.assertIn("agent-only", goal)

            with mock.patch.dict(questions.THRESHOLDS, backlog_goal=decide.Supported(.9)):
                uncovered = self.compare("heldout-validation", split="holdout")
            goal = section(section(uncovered, "## Risk-coverage"), "### backlog_goal")
            self.assertIn("Held-out validation: fail at configured threshold 0.90", goal)
            self.assertIn("Sure N: 0", goal)

            _ = paths[1].write_text(paths[1].read_text().replace(
                'backlog_goal: "2 - Find Collaborators"', 'backlog_goal: "1 - Ship Hana"'))
            missing_titles = [f"Missing heldout validation {number}" for number in range(30)
                              if evaluate.split_for(f"Missing heldout validation {number}.md") == "holdout"][:2]
            missing, request_failed = [self.issue(title) for title in missing_titles]
            _ = self.run_file("heldout-validation", [*entries, {"issue_path": str(request_failed), "error": "fixture failure"}])
            partial = self.compare("heldout-validation", split="holdout")
            self.assertIn("partial", partial.splitlines()[0])
            self.assertIn(missing.stem, section(section(partial, "## Exclusions and incomplete coverage"), "### Unscored issues"))
            self.assertIn(request_failed.stem, section(section(partial, "## Exclusions and incomplete coverage"), "### Failed issues"))
            goal = section(section(partial, "## Risk-coverage"), "### backlog_goal")
            self.assertIn("Held-out validation: partial at configured threshold 0.73", goal)
            self.assertNotIn("Held-out validation: pass", goal)
            self.assertNotIn("Threshold outcome:", partial)

    def test_heldout_checks_the_tuned_supported_policy_and_keeps_other_fields_agent_only(self) -> None:
        titles = [f"Tuned heldout {number}" for number in range(100)
                  if evaluate.split_for(f"Tuned heldout {number}.md") == "holdout"][:2]
        entries = [self.record(self.issue(title)) for title in titles]
        for record, confidence in zip(entries, (.89, .9)):
            for name in ("category", "category_reverse"):
                record["answers"]["main"]["answers"][name] = {
                    "type": "choice", "choice": "feature", "confidence": confidence,
                    "probabilities": {"feature": confidence, "refactor": 1 - confidence},
                }
            # Perfectly confident area answers cannot override Unsupported.
            for name in ("area", "area_reverse"):
                record["answers"]["area"]["answers"][name] = {
                    "type": "choice", "choice": "rendering", "confidence": 1.,
                    "probabilities": {"rendering": 1., "tooling": 0.},
                }
            record["answers"]["area"]["answers"]["area_fits"] = {"type": "noul", "noul": 1.}
            self.refresh_decisions(record)
        _ = self.run_file("tuned-heldout", entries)
        with mock.patch.object(decide, "select_threshold", side_effect=AssertionError("Held-out data must not select thresholds")):
            report = self.compare("tuned-heldout", split="holdout")
        risk = section(report, "## Risk-coverage")
        for field, threshold, count in (("backlog_goal", "0.40", 2), ("category", "0.90", 1),
                                        ("backlog_impact", "0.40", 2), ("backlog_alignment", "0.40", 2)):
            with self.subTest(field=field):
                judgment = section(risk, "### " + field)
                self.assertIn("Held-out validation: pass at configured threshold " + threshold, judgment)
                self.assertIn(f"Sure N: {count};", judgment)
        area = section(risk, "### area")
        self.assertIn("Held-out validation: unsupported", area)
        self.assertIn("agent-only", area)
        # Diagnostic sweeps evaluate a candidate threshold for this field;
        # the configured Unsupported outcome still controls production.
        self.assertEqual([row["Sure N"] for row in table(area, "Threshold")], ["2"] * 12)
        for field in ("backlog_urgency", "backlog_effort"):
            self.assertIn("Held-out validation: report-only", section(risk, "### " + field))
        self.assertNotIn("Threshold outcome:", report)
        recommendations = table(section(report, "## Per-field recommendations and score ≥ 20 results"), "Field")
        area_recommendation = next(row["Recommendation"] for row in recommendations if row["Field"] == "area")
        self.assertIn("agent-only", area_recommendation)

    def test_partial_report_counts_success_failure_and_unattempted_rated_issues(self) -> None:
        scored = self.issue("Target A")
        failed = self.issue("Target B")
        _ = self.issue("Target C")
        _ = self.run_file("partial-fixture", [self.record(scored),
                                               {"issue_path": str(failed), "error": "fixture request failed"}])
        report = self.compare("partial-fixture")
        self.assertIn("partial", report.splitlines()[0].casefold())
        self.assertRegex(report.casefold(), r"scored[^\n]*\b1\b")
        self.assertRegex(report.casefold(), r"failed[^\n]*\b1\b")
        self.assertRegex(report.casefold(), r"unscored[^\n]*\b1\b")

    def test_split_coverage_ignores_failures_and_missing_results_in_the_other_split(self) -> None:
        tune = self.issue("Target A")
        holdout = self.issue("Target G")
        _ = self.issue("Target K")
        _ = self.run_file("split-fixture", [self.record(tune), {"issue_path": str(holdout), "error": "fixture failure"}])
        tune_report = self.compare("split-fixture", split="tune")
        self.assertNotIn("partial", tune_report.splitlines()[0].casefold())
        self.assertIn("Scored: 1; failed: 0; unscored: 0", tune_report)
        holdout_report = self.compare("split-fixture", split="holdout")
        self.assertIn("partial", holdout_report.splitlines()[0].casefold())
        self.assertIn("Scored: 0; failed: 1; unscored: 1", holdout_report)

    def test_ordinal_metrics_and_confusion_use_current_labels_in_each_group(self) -> None:
        paths = [self.issue("Target A", impact=3, score=25),
                 self.issue("Target B", impact=4, score=19, body="Long issue context. " * 20),
                 self.issue("Target C", impact=1, score=20, body="[[target|" + "x" * 199 + "]]")]
        _ = self.run_file("ordinal-fixture", [self.record(path) for path in paths])
        report = self.compare("ordinal-fixture")
        all_group = section(report, "## Group: all (N=3)")
        impact = section(all_group, "#### backlog_impact")
        metrics = table(impact, "N")[0]
        self.assertEqual(metrics["N"], "3")
        for name, expected in (("Exact", 1 / 3), ("Within one", 2 / 3), ("MAE", 1.),
                               ("Quadratic-weighted kappa", 0.), ("RPS", .2041666667)):
            self.assertAlmostEqual(float(metrics[name]), expected, delta=.00006)
        matrix = table(impact, "Label / Jev")
        self.assertEqual([row["3"] for row in matrix], ["1", "0", "1", "1", "0"])
        self.assertEqual(sum(int(row[str(level)]) for row in matrix for level in range(1, 6)), 3)
        for heading in ("## Group: title-only (N=2)", "## Group: score ≥ 20 (N=2)"):
            subgroup = section(section(report, heading), "#### backlog_impact")
            values = table(subgroup, "N")[0]
            self.assertEqual(values["Exact"], "0.5000")
            self.assertEqual(values["Within one"], "0.5000")
            self.assertEqual(values["MAE"], "1.0000")

    def test_probability_metrics_read_raw_answers_instead_of_synthetic_decision_probabilities(self) -> None:
        paths = [self.issue("Target A"), self.issue("Target B"), self.issue("Target C")]
        records = [self.record(path) for path in paths]
        _ = paths[1].write_text(paths[1].read_text().replace("category: feature", "category: enhancement"))
        for record in records:
            for decision in record["decided_fields"].values():
                cast(dict[str, object], decision)["probabilities"] = {"invalid synthetic distribution": 1.}
        _ = self.run_file("probability-fixture", records)
        group = section(self.compare("probability-fixture"), "## Group: all (N=3)")
        category = section(group, "#### category")
        metrics = table(category, "N")[0]
        self.assertEqual(metrics["Accuracy"], "0.6667")
        self.assertAlmostEqual(float(metrics["Brier"]), .51125, delta=.00006)
        buckets = table(category, "Confidence")
        self.assertEqual(len(buckets), 5)
        self.assertEqual([row["N"] for row in buckets], ["0", "0", "0", "0", "3"])
        self.assertEqual(buckets[-1]["Mean confidence"], "0.8000")
        self.assertEqual(buckets[-1]["Accuracy"], "0.6667")
        goal = table(section(group, "#### backlog_goal"), "N")[0]
        self.assertAlmostEqual(float(goal["Brier"]), .05375, delta=.00006)
        impact = table(section(group, "#### backlog_impact"), "N")[0]
        self.assertEqual(impact["RPS"], "0.0125")

    def test_compare_reloads_labels_and_backlog_score_without_rewriting_the_saved_run(self) -> None:
        path = self.issue("Target A")
        run_path = self.run_file("current-labels", [self.record(path)])
        before_run = run_path.read_bytes()
        first = self.compare("current-labels")
        self.assertEqual(table(section(section(first, "## Group: all (N=1)"), "#### backlog_impact"), "N")[0]["Exact"], "1.0000")
        _ = path.write_text(path.read_text().replace("backlog_impact: ⭐⭐⭐\n", "backlog_impact: ⭐\n")
                            .replace("backlog_score: 25", "backlog_score: 19"))
        before_vault = path.read_bytes()
        second = self.compare("current-labels")
        metrics = table(section(section(second, "## Group: all (N=1)"), "#### backlog_impact"), "N")[0]
        self.assertEqual(metrics["Exact"], "0.0000")
        self.assertEqual(metrics["MAE"], "2.0000")
        self.assertIn("## Group: score ≥ 20 (N=0)", second)
        self.assertEqual(path.read_bytes(), before_vault)
        self.assertEqual(run_path.read_bytes(), before_run)

    def test_examples_and_admitted_style_issues_are_listed_and_excluded_from_every_metric(self) -> None:
        target = self.issue("Target A")
        _ = self.run_file("baseline", [self.record(target)])
        baseline = self.compare("baseline")
        example_titles = sorted(questions.HISTORICAL_EFFORT_EXAMPLE_STEMS)
        excluded = [self.issue(title, impact=5) for title in example_titles]
        style = self.issue("Style proposal for review", impact=5, extra='style_rule: "global:rust/names.md"\n')
        self.assertIn(style, [issue.path for issue in vault.load_rated_issues(self.issues).issues])
        _ = self.run_file("excluded-fixture", [self.record(path) for path in [target, *excluded, style]])
        report = self.compare("excluded-fixture")
        self.assertIn("Excluded effort examples: 10; admitted style issues excluded: 1", report)
        for heading in ("## Group: all (N=1)", "## Group: title-only (N=1)", "## Group: score ≥ 20 (N=1)",
                        "## Risk-coverage", "## Largest disagreements (up to 20 per field)"):
            self.assertEqual(section(report, heading), section(baseline, heading))
        exclusions = section(report, "## Exclusions and incomplete coverage")
        for title in example_titles:
            self.assertIn(title, section(exclusions, "### Effort examples"))
        self.assertIn(style.stem, section(exclusions, "### Admitted style issues for review"))

    def test_example_words_and_display_titles_do_not_exclude_a_different_filename(self) -> None:
        path = self.issue("Fix gamepad camera modes crash", extra='title: "add gamepad camera modes to switcher"\n')
        record = self.record(path)
        _ = self.run_file("example-words-success", [record])
        report = self.compare("example-words-success")
        self.assertIn("Scored: 1; failed: 0; unscored: 0; eligible rated issues: 1", report)
        self.assertIn("Excluded effort examples: 0", report)
        area = section(section(report, "## Group: all (N=1)"), "#### area")
        self.assertEqual(table(area, "N")[0]["N"], "1")

        _ = self.run_file("example-words-failure", [{"issue_path": str(path), "error": "fixture request failed"}])
        failed = self.compare("example-words-failure")
        self.assertIn("partial", failed.splitlines()[0].casefold())
        self.assertIn("Scored: 0; failed: 1; unscored: 0; eligible rated issues: 1", failed)
        self.assertIn("Excluded effort examples: 0", failed)
        self.assertIn("fixture request failed", section(failed, "### Failed issues"))

    def test_choice_unfiled_with_disagreeing_orders_keeps_probabilities_and_is_never_sure(self) -> None:
        path = self.issue("Target A")
        record = self.record(path)
        _ = path.write_text(path.read_text().replace('area: "[[rendering]]"', 'area: unfiled'))
        record["payloads"]["area"]["state"] = {"issue": record["payloads"]["main"]["state"]["issue"],
                                                "areas": ["rendering", "unfiled"]}
        record["payloads"]["area"]["questions"] = questions.area_questions(["rendering", "unfiled"])
        area_answers = record["answers"]["area"]["answers"]
        area_answers["area"] = {"type": "choice", "choice": "unfiled", "confidence": .99,
                                "probabilities": {"unfiled": .99, "rendering": .01}}
        area_answers["area_reverse"] = {"type": "choice", "choice": "rendering", "confidence": .51,
                                        "probabilities": {"rendering": .51, "unfiled": .49}}
        self.refresh_decisions(record)
        _ = self.run_file("choice-unfiled", [record])
        report = self.compare("choice-unfiled")
        area = section(section(report, "## Group: all (N=1)"), "#### area")
        metrics = table(area, "N")[0]
        self.assertEqual((metrics["Probability N"], metrics["Accuracy"]), ("1", "1.0000"))
        # Average mass is .74 for unfiled, .26 for rendering: .26² + .26².
        self.assertEqual(metrics["Brier"], "0.1352")
        buckets = table(area, "Confidence")
        self.assertEqual([row["N"] for row in buckets], ["0", "0", "0", "1", "0"])
        self.assertEqual((buckets[3]["Mean confidence"], buckets[3]["Accuracy"]), ("0.7400", "1.0000"))
        risk = table(section(section(report, "## Risk-coverage"), "### area"), "Threshold")
        self.assertEqual([row["Sure N"] for row in risk], ["0"] * 12)

    def test_unfiled_is_exact_match_only_strictly_below_the_absent_evidence_bound(self) -> None:
        path = self.issue("Target A")
        record = self.record(path)
        _ = path.write_text(path.read_text().replace('area: "[[rendering]]"', 'area: unfiled'))
        record["payloads"]["area"]["state"] = {"issue": record["payloads"]["main"]["state"]["issue"],
                                                "areas": ["rendering", "unfiled"]}
        record["payloads"]["area"]["questions"] = questions.area_questions(["rendering", "unfiled"])
        area_answers = record["answers"]["area"]["answers"]
        for name in ("area", "area_reverse"):
            area_answers[name] = {"type": "choice", "choice": "unfiled", "confidence": .8,
                                  "probabilities": {"unfiled": .8, "rendering": .2}}
        for fits, probability_n in ((.1, "0"), (.3, "1")):
            with self.subTest(area_fits=fits):
                area_answers["area_fits"] = {"type": "noul", "noul": fits}
                self.refresh_decisions(record)
                _ = self.run_file("unfiled-boundary", [record])
                report = self.compare("unfiled-boundary")
                area = section(section(report, "## Group: all (N=1)"), "#### area")
                metrics = table(area, "N")[0]
                self.assertEqual((metrics["Accuracy"], metrics["Probability N"]), ("1.0000", probability_n))
                self.assertEqual(sum(int(row["N"]) for row in table(area, "Confidence")), int(probability_n))
                risk = table(section(section(report, "## Risk-coverage"), "### area"), "Threshold")
                if fits == .1:
                    self.assertTrue(metrics["Brier"].startswith("N/A"))
                    self.assertEqual([row["Sure N"] for row in risk], ["1"] * 11 + ["0"])
                else:
                    self.assertEqual(metrics["Brier"], "0.0800")
                    self.assertEqual([row["Sure N"] for row in risk], ["0"] * 12)

    def test_unknown_saved_goal_raises_a_value_error_naming_the_field_and_value(self) -> None:
        path = self.issue("Target A")
        record = self.record(path)
        cast(dict[str, object], record["decided_fields"]["backlog_goal"])["value"] = "Unrecognized goal"
        _ = self.run_file("unknown-goal", [record])
        with self.assertRaisesRegex(ValueError, "backlog_goal.*Unrecognized goal"):
            _ = self.compare("unknown-goal")

    def test_evidence_urgency_and_unfiled_area_contribute_only_exact_match_metrics(self) -> None:
        first = self.issue("Target A")
        second = self.issue("Target B")
        records = [self.record(first), self.record(second)]
        main = records[1]["answers"]["main"]["answers"]
        main["current_harm"] = {"type": "noul", "noul": .95}
        records[1]["answers"]["area"]["answers"]["area_fits"] = {"type": "noul", "noul": .1}
        self.refresh_decisions(records[1])
        _ = second.write_text(second.read_text().replace("backlog_urgency: ⭐\n", "backlog_urgency: ⭐⭐⭐⭐\n")
                              .replace('area: "[[rendering]]"', 'area: unfiled'))
        _ = self.run_file("exact-only", records)
        group = section(self.compare("exact-only"), "## Group: all (N=2)")
        urgency = table(section(group, "#### backlog_urgency"), "N")[0]
        self.assertEqual((urgency["N"], urgency["Ordinal N"], urgency["Exact"]), ("2", "1", "1.0000"))
        self.assertAlmostEqual(float(urgency["RPS"]), .010625, delta=.00006)
        area_section = section(group, "#### area")
        area = table(area_section, "N")[0]
        self.assertEqual((area["N"], area["Probability N"], area["Accuracy"]), ("2", "1", "1.0000"))
        self.assertAlmostEqual(float(area["Brier"]), .03125, delta=.00006)
        self.assertEqual(sum(int(row["N"]) for row in table(area_section, "Confidence")), 1)

    def test_risk_coverage_applies_thresholds_without_admitting_split_or_order_disagreement(self) -> None:
        paths = [self.issue("Target A"), self.issue("Target B", impact=1),
                 self.issue("Target C"), self.issue("Target D")]
        records = [self.record(path) for path in paths]
        self.score_answer(records[1], "impact", {"0": .025, "1": .025, "2": .9, "3": .025, "4": .025})
        self.score_answer(records[2], "impact", {"0": 0., "1": 0., "2": .6, "3": 0., "4": .4})
        records[3]["answers"]["main"]["answers"]["goal_reverse"] = {
            "type": "choice", "choice": "find_collaborators", "confidence": .9,
            "probabilities": {"ship_hana": .1, "find_collaborators": .9, "seek_investors": 0., "business_viability": 0.},
        }
        self.refresh_decisions(records[3])
        _ = self.run_file("risk-fixture", records)
        risk = section(self.compare("risk-fixture"), "## Risk-coverage")
        impact = table(section(risk, "### backlog_impact"), "Threshold")
        self.assertEqual([row["Threshold"] for row in impact],
                         ["0.40", "0.45", "0.50", "0.55", "0.60", "0.65", "0.70", "0.75", "0.80", "0.85", "0.90", "0.95"])
        by_threshold = {row["Threshold"]: row for row in impact}
        for threshold in ("0.40", "0.60", "0.70"):
            self.assertEqual(by_threshold[threshold]["Sure N"], "3")
            self.assertEqual(by_threshold[threshold]["Coverage"], "0.7500")
            self.assertEqual(by_threshold[threshold]["Exact error"], "0.3333")
            self.assertEqual(by_threshold[threshold]["Beyond-one error"], "0.3333")
        self.assertEqual(by_threshold["0.75"]["Sure N"], "1")
        self.assertEqual(by_threshold["0.75"]["Exact error"], "1.0000")
        self.assertEqual(by_threshold["0.95"]["Sure N"], "0")
        self.assertEqual(by_threshold["0.95"]["Coverage"], "0.0000")
        self.assertTrue(by_threshold["0.95"]["Exact error"].startswith("N/A"))
        goal = table(section(risk, "### backlog_goal"), "Threshold")[0]
        self.assertEqual((goal["Sure N"], goal["Coverage"], goal["Exact error"]), ("3", "0.7500", "0.0000"))

    def test_repeat_rates_compare_only_successes_with_identical_content_version_and_model(self) -> None:
        paths = [self.issue(f"Target {letter}") for letter in "ABCDE"]
        records = [self.record(path) for path in paths]
        second = copy.deepcopy(records[:4])
        self.score_answer(second[0], "impact", {"0": 0., "1": 0., "2": 0., "3": 0., "4": 1.})
        second[1]["content_hash"] = "changed-content"
        second[2]["question_set_version"] = "changed-version"
        second[3]["model"] = "changed-model"
        _ = self.run_file("repeat-first", records)
        _ = self.run_file("repeat-second", [*second, {"issue_path": str(paths[4]), "error": "fixture failed repeat"}])
        repeat = section(self.compare("repeat-first", repeat_run_id="repeat-second"), "## Repeat check")
        self.assertIn("Comparable issues: 1; changed content/model/version: 3; missing second successes: 1", repeat)
        rows = {row["Field"]: row for row in table(repeat, "Field")}
        self.assertEqual((rows["backlog_impact"]["N"], rows["backlog_impact"]["Changed"], rows["backlog_impact"]["Change rate"]),
                         ("1", "1", "1.0000"))
        self.assertEqual(rows["backlog_effort"]["Change rate"], "0.0000")

    def test_repeat_pairs_require_the_same_variant_name_and_question_definitions(self) -> None:
        path = self.issue("Target A")
        production = self.record(path)
        explicit_production = copy.deepcopy(production)
        explicit_production["variant_set"] = "production"
        explicit_production["variant_definitions"] = {}
        variant = copy.deepcopy(production)
        variant["variant_set"] = "benefit_scope"
        replacement = copy.deepcopy(questions.main_questions()["impact"])
        replacement["instructions"] = "How large is the benefit once `issue` is complete?"
        variant["variant_definitions"] = {"impact": replacement}
        other_name = copy.deepcopy(variant)
        other_name["variant_set"] = "another_scope"
        other_wording = copy.deepcopy(variant)
        other_wording["variant_definitions"]["impact"]["instructions"] = "How much does completing `issue` benefit its users?"
        repeated_variant = copy.deepcopy(variant)
        self.score_answer(repeated_variant, "impact", {"0": 0., "1": 0., "2": 0., "3": 0., "4": 1.})
        choice_variant = copy.deepcopy(production)
        choice_variant["variant_set"] = "category_scope"
        category = copy.deepcopy(questions.main_questions()["category"])
        assert category["type"] == "choice"
        choice_variant["variant_definitions"] = {"category": category}
        repeated_choice = copy.deepcopy(choice_variant)
        reversed_choice = copy.deepcopy(choice_variant)
        reversed_category = copy.deepcopy(category)
        reversed_category["criteria"] = dict(reversed(list(category["criteria"].items())))
        reversed_choice["variant_definitions"] = {"category": reversed_category}
        cases = ((production, variant, 0, 0), (variant, other_name, 0, 0),
                 (variant, other_wording, 0, 0), (variant, repeated_variant, 1, 1),
                 (production, explicit_production, 1, 0),
                 (choice_variant, reversed_choice, 0, 0), (choice_variant, repeated_choice, 1, 0))
        for first, second, comparable, changes in cases:
            with self.subTest(first=first.get("variant_set"), second=second.get("variant_set"), comparable=comparable, changes=changes):
                _ = self.run_file("wording-first", [first])
                _ = self.run_file("wording-second", [second])
                report = section(self.compare("wording-first", repeat_run_id="wording-second"), "## Repeat check")
                self.assertIn(f"Comparable issues: {comparable};", report)
                self.assertIn(f"changed question definitions: {1 - comparable};", report)
                self.assertIn(f"not comparable: {1 - comparable}.", report)
                impact = next(row for row in table(report, "Field") if row["Field"] == "backlog_impact")
                self.assertEqual((impact["N"], impact["Changed"]), (str(comparable), str(changes)))
                if comparable:
                    self.assertEqual(impact["Change rate"], "1.0000" if changes else "0.0000")
                else:
                    self.assertTrue(impact["Change rate"].startswith("N/A"))

    def test_ordering_uses_imported_goal_parser_and_score_formula_with_current_backlog_scores(self) -> None:
        paths = [self.issue("Target A", score=30), self.issue("Target B", score=10), self.issue("Target C", score=20)]
        records = [self.record(path) for path in paths]
        self.score_answer(records[0], "impact", {"0": 1., "1": 0., "2": 0., "3": 0., "4": 0.})
        self.score_answer(records[2], "impact", {"0": 0., "1": 0., "2": 0., "3": 0., "4": 1.})
        _ = self.run_file("ordering", records)
        with mock.patch.object(renumber, "parse_goals", wraps=renumber.parse_goals) as parser, \
                mock.patch.object(renumber, "calculate_score", wraps=renumber.calculate_score) as formula:
            report = self.compare("ordering")
        parser.assert_called_once()
        self.assertGreaterEqual(formula.call_count, 3)
        # Predicted scores 17,23,29 rank opposite current scores 30,10,20 at rho=-.5.
        group = section(report, "## Group: all (N=3)")
        self.assertIn("Ordering pairs: 3", group)
        self.assertIn("Spearman correlation: -0.5000", group)

    def test_disagreement_list_keeps_the_twenty_largest_gaps_in_descending_order(self) -> None:
        paths = [self.issue(f"Disagreement {number:02}", impact=1) for number in range(25)]
        records = [self.record(path) for path in paths]
        self.score_answer(records[23], "impact", {"0": 0., "1": 0., "2": 0., "3": 1., "4": 0.})
        self.score_answer(records[24], "impact", {"0": 0., "1": 0., "2": 0., "3": 0., "4": 1.})
        _ = self.run_file("disagreements", records)
        disagreements = section(self.compare("disagreements"), "## Largest disagreements (up to 20 per field)")
        rows = table(section(disagreements, "### backlog_impact"), "Issue")
        self.assertEqual(len(rows), 20)
        self.assertEqual([row["Issue"] for row in rows[:2]], ["Disagreement 24", "Disagreement 23"])
        self.assertEqual([row["Gap"] for row in rows[:2]], ["4.0000", "3.0000"])
        self.assertEqual({row["Issue"] for row in rows[2:]}, {f"Disagreement {number:02}" for number in range(18)})

    def test_compare_cli_passes_split_and_repeat_to_a_report_built_from_saved_answers(self) -> None:
        tune = self.issue("Target A")
        holdout = self.issue("Target G")
        records = [self.record(tune), self.record(holdout)]
        _ = self.run_file("cli-first", records)
        _ = self.run_file("cli-repeat", records)
        original_compare = evaluate.compare

        def isolated_compare(run_id: str, *, split: Literal["tune", "holdout", "all"] = "all",
                             repeat_run_id: str = "", baseline_run_id: str = "") -> Path:
            return original_compare(run_id, split=split, repeat_run_id=repeat_run_id,
                                    issues_dir=self.issues, state_dir=self.state, goals_path=self.goals, baseline_run_id=baseline_run_id)

        stdout = io.StringIO()
        with mock.patch.object(score_issues, "compare", side_effect=isolated_compare), contextlib.redirect_stdout(stdout):
            code = score_issues.main(["compare", "cli-first", "--split", "tune", "--repeat", "cli-repeat", "--baseline", "cli-repeat"])
        self.assertEqual(code, 0)
        report_path = Path(stdout.getvalue().strip())
        self.assertEqual(report_path, self.state / "reports" / "cli-first-tune.md")
        report = report_path.read_text()
        self.assertIn("Scored: 1; failed: 0; unscored: 0", report)
        self.assertIn("Comparable issues: 1", section(report, "## Repeat check"))
        self.assertIn("Shared cohort with baseline cli-repeat: 1 issues", report)


if __name__ == "__main__":
    _ = unittest.main()
