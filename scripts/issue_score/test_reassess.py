"""Reassessment fixtures exercise saved judgments without a live service or vault."""
from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
import tempfile
from typing import cast, override
import unittest
from unittest import mock

from scripts.issue_score import client, decide, evaluate, questions, records, score_issues, vault
from scripts.issue_score.test_decide import choice_answer, score_answer

FIXTURES = Path(__file__).with_name("fixtures")


class ReassessmentFixture(unittest.TestCase):
    root: Path = Path()
    issues: Path = Path()
    state: Path = Path()
    goals: Path = Path()
    definitions_path: Path = Path()
    definitions: dict[str, str] = {}

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
        self.definitions_path = self.root / "areas.md"
        self.definitions = {"rendering": "Drawing scenes.", "tooling": "Building projects."}
        _ = self.definitions_path.write_text(
            "area | definition\n--- | ---\nrendering | Drawing scenes.\ntooling | Building projects.\n")
        _ = self.enterContext(mock.patch.object(Path, "home", return_value=self.root))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "fixture-only"})))
        _ = self.enterContext(mock.patch.object(client, "urlopen", side_effect=AssertionError("Live HTTP forbidden")))

    def issue(self, name: str, *, goal: str = "1 - Ship Hana", alignment: int = 1,
              impact: int = 1, score: int = 5, extra: str = "", body: str = "User-visible change.\n") -> Path:
        path = self.issues / f"{name}.md"
        _ = path.write_text(
            '---\nstatus: open\nproject: "[[hana]]"\n'
            + f'backlog_goal: "{goal}"\nbacklog_alignment: "{"⭐" * alignment}"\n'
            + f'backlog_impact: "{"⭐" * impact}"\nbacklog_urgency: "⭐"\nbacklog_effort: "⭐"\n'
            + f'backlog_score: {score}\ncategory: idea\narea: "[[rendering]]"\n'
            + extra + '---\n' + body, encoding="utf-8")
        return path

    def loaded(self, path: Path) -> vault.Issue:
        return next(issue for issue in vault.load_scoring_issues(self.issues, rules_dirs=(self.root / "rules",))
                    if issue.path == path)

    def judgment(self, path: Path, *, main_fixture: str = "main_response.json") -> records.ScoredIssue:
        issue = self.loaded(path)
        main = cast(client.Response, json.loads((FIXTURES / main_fixture).read_text()))
        area = cast(client.Response, json.loads((FIXTURES / "area_response.json").read_text()))
        state = vault.build_state(issue)
        areas = vault.area_vocabulary(vault.load_open_issues(self.issues))
        area_questions = questions.area_questions(areas, self.definitions)
        for name in ("area", "area_reverse"):
            answer = area["answers"][name]
            assert answer["type"] == "choice"
            probabilities = {name: answer["probabilities"].get(name, 0.) for name in areas}
            winner = max(probabilities, key=lambda name: probabilities[name])
            probabilities[winner] += 1. - sum(probabilities.values())
            area["answers"][name] = choice_answer(probabilities)
        main = client.parse_response(main, questions.main_questions())
        area = client.parse_response(area, area_questions)
        return {
            "issue_path": str(path), "content_hash": score_issues.content_hash(issue, areas, self.definitions),
            "question_set_version": questions.QUESTION_SET_VERSION, "model": questions.MODEL,
            "answers": {"main": main, "area": area},
            "payloads": {
                "main": {"model": questions.MODEL, "state": state, "questions": questions.main_questions()},
                "area": {"model": questions.MODEL, "state": {**state, "areas": areas}, "questions": area_questions},
            },
            "decided_fields": {name: asdict(decision) for name, decision in
                               decide.decide_answers(main["answers"], area["answers"]).items()},
        }

    def run_file(self, name: str, rows: Sequence[records.ScoredIssue | records.FailedIssue]) -> str:
        _ = (self.state / "runs" / f"{name}.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
        return name

    def reassess(self, run_id: str) -> tuple[str, records.Reassessment]:
        path = score_issues.reassess(run_id, issues_dir=self.issues, state_dir=self.state,
                                     area_definitions_path=self.definitions_path, rules_dirs=(self.root / "rules",),
                                     goals_path=self.goals)
        self.assertEqual(path, self.state / "reports" / f"{run_id}-reassess.md")
        return path.read_text(), cast(records.Reassessment, json.loads(path.with_suffix(".json").read_text()))


class ReassessTests(ReassessmentFixture):
    def test_review_groups_unrated_and_default_issues_first_and_sorts_largest_rated_disagreements(self) -> None:
        close = self.issue("A close", alignment=4, impact=3, score=25)
        far = self.issue("Z far", goal="4 - Business Viability", alignment=1, impact=1, score=11)
        unrated = self.issue("Unrated")
        _ = unrated.write_text(unrated.read_text().replace('backlog_goal: "1 - Ship Hana"', 'backlog_goal:')
                               .replace('backlog_impact: "⭐"', 'backlog_impact:').replace('backlog_score: 5', 'backlog_score:'))
        default = self.issue("Default style", alignment=2, impact=2, extra=(
            'style_rule: "global:rust/names.md"\nstyle_count: 2\n'), body="")
        rows = [self.judgment(path) for path in (close, far, unrated, default)]
        run_id = self.run_file("grouped", rows)
        before = {path: path.read_bytes() for path in self.issues.glob("*.md")}
        markdown, review = self.reassess(run_id)
        self.assertTrue(review["complete"])
        self.assertEqual([row["group"] for row in review["rows"][:2]], ["unrated", "default-rated"])
        rated = [row for row in review["rows"] if row["group"] == "rated"]
        self.assertEqual([row["issue_path"] for row in rated], [str(far), str(close)])
        by_path = {row["issue_path"]: row for row in review["rows"]}
        self.assertEqual(by_path[str(close)]["current"]["backlog_alignment"], "⭐⭐⭐⭐")
        self.assertEqual(by_path[str(far)]["proposed"]["backlog_alignment"], "⭐⭐⭐⭐")
        self.assertIn("## Unrated and default-rated", markdown)
        self.assertIn("## Taken", markdown)
        self.assertIn("## Routed to user by band", markdown)
        self.assertIn("## Score >= 20 — field results", markdown)
        high_group = markdown.split("## Score >= 20 — field results", 1)[1].split("## Failed issues", 1)[0]
        self.assertIn("Issues: 1", high_group)
        self.assertIn("| backlog_impact | 1 | 0 |", high_group)
        for path in (unrated, default):
            self.assertTrue({"category", "area", "backlog_urgency", "backlog_effort"}.issubset(by_path[str(path)]["user"]))
        self.assertEqual(by_path[str(default)]["missing_rules"], ["global:rust/names.md"])
        self.assertIn("MissingStyleRule: global:rust/names.md", markdown)
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_unrated_and_default_issues_require_agent_values_before_provisional_score_and_band(self) -> None:
        unrated = self.issue("Agent fills missing values")
        _ = unrated.write_text(unrated.read_text().replace('backlog_impact: "⭐"', 'backlog_impact:')
                               .replace('backlog_score: 5', 'backlog_score:'))
        default = self.issue("Agent reviews default style", alignment=2, impact=2,
                             extra='style_rule: "global:rust/names.md"\nstyle_count: 2\n', body="")
        run_id = self.run_file("agent-values", [self.judgment(path) for path in (unrated, default)])
        before = {path: path.read_bytes() for path in (unrated, default)}
        markdown, pending = self.reassess(run_id)
        required = {"category", "area", "backlog_urgency", "backlog_effort"}
        for row in pending["rows"]:
            self.assertEqual(row["agent_proposed"], {})
            routing = row["routing"]
            self.assertEqual(routing["status"], "awaiting-agent-proposals")
            assert routing["status"] == "awaiting-agent-proposals"
            self.assertEqual(set(routing["missing_fields"]), required)
        band_section = markdown.split("## Routed to user by band", 1)[1].split("## Score >= 20 — field results", 1)[0]
        self.assertNotIn(str(unrated), band_section)
        self.assertNotIn(str(default), band_section)
        form_path = self.state / "reports" / f"{run_id}-agent-proposals.json"
        form = cast(records.AgentProposals, json.loads(form_path.read_text()))
        self.assertEqual(form["run_id"], run_id)
        self.assertEqual({entry["issue_path"] for entry in form["issues"]}, {str(path) for path in before})
        for entry in form["issues"]:
            self.assertEqual(entry["fields"], dict.fromkeys(required, ""))
            entry["fields"].update({"category": "research", "area": "unfiled", "backlog_urgency": "⭐⭐⭐⭐"})
        _ = form_path.write_text(json.dumps(form, ensure_ascii=False))
        _, incomplete = self.reassess(run_id)
        for row in incomplete["rows"]:
            routing = row["routing"]
            self.assertEqual(routing["status"], "awaiting-agent-proposals")
            assert routing["status"] == "awaiting-agent-proposals"
            self.assertEqual(routing["missing_fields"], ["backlog_effort"])
        for entry in form["issues"]:
            entry["fields"]["backlog_effort"] = "⭐⭐⭐⭐⭐"
        _ = form_path.write_text(json.dumps(form, ensure_ascii=False))
        markdown, completed = self.reassess(run_id)
        for row in completed["rows"]:
            self.assertEqual(row["agent_proposed"], {"category": "research", "area": "unfiled",
                                                   "backlog_urgency": "⭐⭐⭐⭐", "backlog_effort": "⭐⭐⭐⭐⭐"})
            self.assertEqual(row["routing"], {"status": "ready", "score": 40., "band": "Score >= 20"})
            self.assertTrue(required.issubset(row["user"]))
            self.assertTrue(required.isdisjoint(row["taken"]))
        high_band = markdown.split("### Score >= 20", 1)[1].split("### Score 10–19", 1)[0]
        self.assertIn(f"{unrated} (score 40)", high_band)
        self.assertIn(f"{default} (score 40)", high_band)
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_partial_review_keeps_failed_and_unscored_issues_in_separate_lists(self) -> None:
        scored = self.issue("Scored")
        failed = self.issue("Failed API")
        missing = self.issue("Unscored after budget stop")
        run_id = self.run_file("partial", [self.judgment(scored),
                              {"issue_path": str(failed), "error": "fixture budget exhausted"}])
        markdown, review = self.reassess(run_id)
        self.assertFalse(review["complete"])
        self.assertIn("(partial)", markdown.splitlines()[0])
        self.assertEqual(review["failed"], [{"issue_path": str(failed), "error": "fixture budget exhausted"}])
        self.assertEqual(review["unscored"], [str(missing)])
        failed_section, unscored_section = markdown.split("## Failed issues", 1)[1].split("## Unscored issues", 1)
        self.assertIn(str(failed), failed_section)
        self.assertNotIn(str(missing), failed_section)
        self.assertIn(str(missing), unscored_section)
        self.assertNotIn(str(failed), unscored_section)

    def test_interrupted_agent_form_replacement_preserves_filled_choices_for_rerun(self) -> None:
        target = self.issue("Preserve completed agent choices")
        _ = target.write_text(target.read_text().replace('backlog_impact: "⭐"', 'backlog_impact:'))
        run_id = self.run_file("atomic-agent-form", [self.judgment(target)])
        _ = self.reassess(run_id)
        form_path = self.state / "reports" / f"{run_id}-agent-proposals.json"
        form = cast(records.AgentProposals, json.loads(form_path.read_text()))
        choices = {"category": "research", "area": "unfiled", "backlog_urgency": "⭐⭐⭐⭐", "backlog_effort": "⭐⭐⭐⭐⭐"}
        form["issues"][0]["fields"].update(choices)
        _ = form_path.write_text(json.dumps(form, ensure_ascii=False))
        before = form_path.read_bytes()
        replace = os.replace

        def stop_before_replace(source: Path, destination: Path) -> None:
            if destination == form_path:
                self.assertEqual(source.parent, form_path.parent)
                self.assertNotEqual(source, form_path)
                self.assertTrue(source.is_file())
                synced.assert_called()
                raise SystemExit("fixture stops before agent form replacement")
            replace(source, destination)

        with mock.patch.object(os, "fsync", wraps=os.fsync) as synced:
            with mock.patch.object(os, "replace", side_effect=stop_before_replace):
                with self.assertRaisesRegex(SystemExit, "before agent form replacement"):
                    _ = self.reassess(run_id)
        self.assertEqual(form_path.read_bytes(), before)
        self.assertEqual(json.loads(form_path.read_text()), form)
        _, review = self.reassess(run_id)
        self.assertTrue(review["complete"])
        self.assertEqual(review["rows"][0]["agent_proposed"], choices)
        self.assertEqual(review["rows"][0]["routing"], {"status": "ready", "score": 40., "band": "Score >= 20"})
        self.assertEqual(json.loads(form_path.read_text()), form)

    def test_only_an_unterminated_final_run_fragment_is_unscored_instead_of_fatal(self) -> None:
        scored, stopped = self.issue("Completed JSONL record"), self.issue("Interrupted JSONL record")
        run_id = self.run_file("interrupted-jsonl", [self.judgment(scored)])
        run_path = self.state / "runs" / f"{run_id}.jsonl"
        complete_line = run_path.read_text()
        fragment = '{"issue_path": ' + json.dumps(str(stopped)) + ', "answers": '
        tails = (fragment.encode(), fragment.encode() + b'{"label":"' + "⭐".encode()[:2])
        for tail in tails:
            with self.subTest(tail=tail):
                _ = run_path.write_bytes(complete_line.encode() + tail)
                markdown, review = self.reassess(run_id)
                self.assertFalse(review["complete"])
                self.assertEqual([row["issue_path"] for row in review["rows"]], [str(scored)])
                self.assertEqual(review["failed"], [])
                self.assertEqual(review["unscored"], [str(stopped)])
                self.assertIn(str(stopped), markdown.split("## Unscored issues", 1)[1])
        for corruption in ((complete_line + fragment + "\n").encode(),
                           (complete_line + fragment + "\n" + complete_line).encode(),
                           b'\xff\n' + complete_line.encode()):
            with self.subTest(corruption=corruption):
                _ = run_path.write_bytes(corruption)
                with self.assertRaises(ValueError):
                    _ = self.reassess(run_id)

    def test_every_byte_truncation_of_the_final_record_preserves_completed_rows_and_leaves_it_unscored(self) -> None:
        scored, stopped = self.issue("Complete before every cut"), self.issue("Stopped at every byte")
        completed = self.judgment(scored)
        interrupted = self.judgment(stopped)
        run_id = self.run_file("every-byte-cut", [completed])
        run_path = self.state / "runs" / f"{run_id}.jsonl"
        complete_line = run_path.read_bytes()
        final_record = json.dumps(interrupted, ensure_ascii=False).encode("utf-8")
        for offset in range(len(final_record)):
            _ = run_path.write_bytes(complete_line + final_record[:offset])
            try:
                run = evaluate.load_run(run_path, allow_partial_last_line=True)
            except ValueError as error:
                self.fail(f"Interrupted final record at byte {offset} must be ignored: {error}")
            self.assertEqual(run.scored, {str(scored): completed}, f"cut at byte {offset}")
            self.assertEqual(run.failed, {}, f"cut at byte {offset}")
        numeric_cut = final_record.index(b'"confidence": 0.') + len(b'"confidence": 0.')
        _ = run_path.write_bytes(complete_line + final_record[:numeric_cut])
        markdown, review = self.reassess(run_id)
        self.assertFalse(review["complete"])
        self.assertEqual([row["issue_path"] for row in review["rows"]], [str(scored)])
        self.assertEqual(review["failed"], [])
        self.assertEqual(review["unscored"], [str(stopped)])
        self.assertIn(str(stopped), markdown.split("## Unscored issues", 1)[1])
        _ = run_path.write_bytes(complete_line + final_record)
        self.assertEqual(evaluate.load_run(run_path, allow_partial_last_line=True).scored,
                         {str(scored): completed, str(stopped): interrupted})

    def test_agent_can_propose_a_defined_area_that_no_issue_uses(self) -> None:
        unused_area = "documentation"
        self.definitions[unused_area] = "Writing user and contributor documentation."
        _ = self.definitions_path.write_text(self.definitions_path.read_text()
                                            + unused_area + " | " + self.definitions[unused_area] + "\n")
        target = self.issue("First issue in a defined area")
        _ = target.write_text(target.read_text().replace('backlog_impact: "⭐"', 'backlog_impact:'))
        self.assertNotIn(unused_area, vault.area_vocabulary(vault.load_open_issues(self.issues)))
        run_id = self.run_file("unused-defined-area", [self.judgment(target)])
        _ = self.reassess(run_id)
        form_path = self.state / "reports" / f"{run_id}-agent-proposals.json"
        form = cast(records.AgentProposals, json.loads(form_path.read_text()))
        choices = {"category": "research", "area": unused_area, "backlog_urgency": "⭐⭐⭐⭐", "backlog_effort": "⭐⭐⭐⭐⭐"}
        form["issues"][0]["fields"].update(choices)
        _ = form_path.write_text(json.dumps(form, ensure_ascii=False))
        markdown, review = self.reassess(run_id)
        self.assertTrue(review["complete"])
        self.assertEqual(review["rows"][0]["agent_proposed"], choices)
        self.assertEqual(review["rows"][0]["routing"]["status"], "ready")
        self.assertIn("area " + unused_area, markdown)

    def test_routing_rebuilds_raw_answers_at_threshold_equality_and_ignores_stale_saved_certainty(self) -> None:
        equal = self.issue("Exactly at goal threshold")
        disagreement = self.issue("Disagreeing goal orders")
        split = self.issue("Split impact")
        below = self.issue("Below goal threshold")
        raw = [self.judgment(path) for path in (equal, disagreement, split, below)]
        for row in raw:
            for name in ("goal", "goal_reverse"):
                row["answers"]["main"]["answers"][name] = choice_answer({
                    "ship_hana": .4, "find_collaborators": .3, "seek_investors": .2, "business_viability": .1})
            row["answers"]["main"]["answers"]["alignment_ship_hana"] = score_answer({"0": 0., "1": 0., "2": .05, "3": .95, "4": 0.})
            row["decided_fields"] = {field: {"value": "stale", "certainty": "sure", "confidence": 1.}
                                      for field in questions.THRESHOLDS}
        raw[1]["answers"]["main"]["answers"]["goal_reverse"] = choice_answer({
            "ship_hana": .3, "find_collaborators": .4, "seek_investors": .2, "business_viability": .1})
        raw[2]["answers"]["main"]["answers"]["impact"] = score_answer({"0": .3, "1": 0., "2": 0., "3": 0., "4": .7})
        for name in ("goal", "goal_reverse"):
            raw[3]["answers"]["main"]["answers"][name] = choice_answer({
                "ship_hana": .399, "find_collaborators": .3, "seek_investors": .2, "business_viability": .101})
        _, review = self.reassess(self.run_file("routing", raw))
        rows = {row["issue_path"]: row for row in review["rows"]}
        self.assertEqual(set(rows[str(equal)]["taken"]), set(questions.JEV_FIELDS))
        self.assertEqual(rows[str(equal)]["proposed"]["backlog_alignment"], "⭐⭐⭐⭐")
        for path in (disagreement, below):
            self.assertIn("backlog_goal", rows[str(path)]["user"])
            self.assertIn("backlog_alignment", rows[str(path)]["user"])
        self.assertIn("backlog_impact", rows[str(split)]["user"])
        for row in rows.values():
            self.assertTrue({"category", "area", "backlog_urgency", "backlog_effort"}.issubset(row["user"]))

    def test_review_preserves_both_goal_alignments_and_distinguishes_unfiled_origins(self) -> None:
        unsure = self.issue("Unsure goal")
        fallback = self.issue("Derived unfiled")
        choice = self.issue("Choice unfiled")
        _ = choice.write_text(choice.read_text().replace('area: "[[rendering]]"', 'area: "[[unfiled]]"'))
        rows = [self.judgment(unsure, main_fixture="proposal_unsure_goal.json"), self.judgment(fallback), self.judgment(choice)]
        rows[1]["answers"]["area"]["answers"]["area_fits"] = {"type": "noul", "noul": .05}
        for name in ("area", "area_reverse"):
            rows[2]["answers"]["area"]["answers"][name] = choice_answer({"unfiled": .95, "rendering": .05})
        markdown, _ = self.reassess(self.run_file("origins", rows))
        def issue_line(path: Path) -> str:
            return next(line for line in markdown.splitlines() if line.startswith("| [" + path.stem + "]"))
        self.assertIn("1 - Ship Hana (alignment ⭐⭐⭐⭐", issue_line(unsure))
        self.assertIn("2 - Find Collaborators (alignment ⭐⭐", issue_line(unsure))
        self.assertIn("area unfiled (hint: rendering)", issue_line(fallback))
        self.assertIn("area unfiled", issue_line(choice))
        self.assertNotIn("hint:", issue_line(choice))

    def test_changed_definitions_make_review_partial_and_require_rescoring(self) -> None:
        path = self.issue("Stale definition")
        run_id = self.run_file("stale", [self.judgment(path)])
        _ = self.definitions_path.write_text(self.definitions_path.read_text().replace("Drawing scenes.", "Drawing GPU scenes."))
        markdown, review = self.reassess(run_id)
        self.assertFalse(review["complete"])
        self.assertEqual(review["rows"], [])
        self.assertIn(str(path), markdown)
        self.assertIn("rescore", markdown)

    def test_reassess_cli_exposes_the_generated_review_path(self) -> None:
        import contextlib
        import io
        path = self.issue("CLI")
        run_id = self.run_file("cli", [self.judgment(path)])
        original = score_issues.reassess
        def isolated(run_id: str) -> Path:
            return original(run_id, issues_dir=self.issues, state_dir=self.state,
                            area_definitions_path=self.definitions_path, rules_dirs=(self.root / "rules",),
                            goals_path=self.goals)
        output = io.StringIO()
        with mock.patch.object(score_issues, "reassess", side_effect=isolated), contextlib.redirect_stdout(output):
            code = score_issues.main(["reassess", run_id])
        self.assertEqual(code, 0)
        self.assertEqual(Path(output.getvalue().strip()), self.state / "reports" / "cli-reassess.md")
