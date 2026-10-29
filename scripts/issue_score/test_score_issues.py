"""Replay the score pipeline and cache without touching the user's vault or state."""
from __future__ import annotations

import contextlib
import gzip
import io
import json
import math
import os
import sys
import tempfile
import threading
import unittest
from collections.abc import Callable, Mapping
from email.message import Message
from pathlib import Path
from typing import Literal, TextIO, cast, override
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request

from scripts.issue_score import client, questions, score_issues, vault
from scripts.issue_score import records as run_records

# proposals_table.py imports its production neighbors by bare name.
PRODUCTION_DIR = str(Path(__file__).resolve().parents[1] / "production")
if PRODUCTION_DIR not in sys.path:
    sys.path.insert(0, PRODUCTION_DIR)
from scripts.production.issues import Issue as ProposalIssue
from scripts.production.proposals_table import proposal_score

# Hang guards allow for load 136 on natedev (2026-10-29).
TEST_HANG_GUARD_SECS = 300

FIXTURES = Path(__file__).with_name("fixtures")


class FixtureHTTPResponse(io.BytesIO):
    headers: Message
    status: int = 200

    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.headers = Message()


class ScoreIssuesTests(unittest.TestCase):
    root: Path = Path()
    issues: Path = Path()
    state: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.issues = self.root / "vault" / "issues"
        self.issues.mkdir(parents=True)
        self.state = self.root / "state"
        _ = self.enterContext(mock.patch.object(Path, "home", return_value=self.root))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "fixture-key"})))
        _ = self.enterContext(mock.patch.object(client, "urlopen", side_effect=AssertionError("Live HTTP forbidden")))
        definitions_path = self.root / "vault" / "conf" / "issue-areas.md"
        definitions_path.parent.mkdir()
        _ = definitions_path.write_text(
            "| area | definition | issues |\n| --- | --- | --- |\n"
            + "| unused-fixture-area | A definition outside the fixture vocabulary. | 1 |\n",
            encoding="utf-8",
        )
        read_definitions = vault.load_area_definitions

        def temporary_definitions(path: Path = vault.AREA_DEFINITIONS_PATH) -> dict[str, str]:
            if path == vault.AREA_DEFINITIONS_PATH:
                path = self.root / "vault" / "conf" / "issue-areas.md"
            return read_definitions(path)

        _ = self.enterContext(mock.patch.object(vault, "load_area_definitions", side_effect=temporary_definitions))

    def issue(self, name: str, *, area: str = "rendering", extra: str = "") -> Path:
        path = self.issues / (name + ".md")
        _ = path.write_text(
            '---\nstatus: open\nproject: "[[hana]]"\n'
            + 'backlog_goal: "1 - Ship Hana"\nbacklog_alignment: ⭐\nbacklog_impact: ⭐\n'
            + 'backlog_urgency: ⭐\nbacklog_effort: ⭐\ncategory: idea\n'
            + f'area: "[[{area}]]"\n' + extra + '---\nRepair [[render graph|the graph]].\n',
            encoding="utf-8",
        )
        return path

    def replay(self, request: Request, timeout: float = 60) -> FixtureHTTPResponse:
        _ = timeout
        payload = cast(dict[str, object], json.loads(gzip.decompress(cast(bytes, request.data))))
        state = cast(dict[str, object], payload["state"])
        fixture = "area_response.json" if "areas" in state else "main_response.json"
        return FixtureHTTPResponse((FIXTURES / fixture).read_bytes())

    def score(self, *, only: str = "Target*", limit: int = 0, model: str = "jev-1.14.0") -> Path:
        return score_issues.score(only, limit, issues_dir=self.issues, state_dir=self.state, model=model).result_path

    def records(self, path: Path) -> list[run_records.ScoredIssue]:
        return [cast(run_records.ScoredIssue, json.loads(line)) for line in path.read_text().splitlines()]

    def proposal(self, project: str = "diegetic", title: str = "Repair graph", body: str = "") -> str:
        return score_issues.propose(project, title, body, issues_dir=self.issues, state_dir=self.state)

    def proposal_cli(self, arguments: list[str]) -> tuple[int, str, str]:
        original_propose = score_issues.propose

        def isolated_propose(project: str, title: str, body: str = "") -> str:
            return original_propose(project, title, body, issues_dir=self.issues, state_dir=self.state)

        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(score_issues, "propose", side_effect=isolated_propose), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = score_issues.main(["propose", *arguments])
        return code, stdout.getvalue(), stderr.getvalue()

    def proposal_replay(self, main: client.Response, area: client.Response) -> Callable[[Request, float], FixtureHTTPResponse]:
        def replay(request: Request, timeout: float = 60) -> FixtureHTTPResponse:
            _ = timeout
            payload = cast(run_records.RequestPayload, json.loads(gzip.decompress(cast(bytes, request.data))))
            response = area if "areas" in payload["state"] else main
            return FixtureHTTPResponse(json.dumps(response).encode())
        return replay

    def test_propose_cli_prints_the_pinned_confirmation_line_and_leaves_vault_untouched(self) -> None:
        _ = self.issue("Vocabulary rendering")
        _ = self.issue("Vocabulary tooling", area="tooling")
        definitions_path = self.root / "vault" / "conf" / "issue-areas.md"
        _ = definitions_path.write_text(
            "| area | definition | issues |\n| --- | --- | --- |\n"
            + "| rendering | Drawing scenes and managing render passes. | 1 |\n", encoding="utf-8")
        before = {path: path.read_bytes() for path in self.issues.glob("*.md")}
        body_path = self.root / "body.md"
        _ = body_path.write_text("Repair [[render graph|the graph]].\n", encoding="utf-8")
        with mock.patch.object(client, "urlopen", side_effect=self.replay) as http:
            code, stdout, stderr = self.proposal_cli([
                "--project", "diegetic", "--title", "Repair graph", "--body-file", str(body_path)])

        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(stdout, (FIXTURES / "proposal_expected.txt").read_text(encoding="utf-8"))
        self.assertEqual(http.call_count, 2)
        payloads = [cast(run_records.RequestPayload, json.loads(gzip.decompress(cast(bytes, call.args[0].data))))
                    for call in http.call_args_list]
        main_state = payloads[0]["state"]
        self.assertEqual(set(main_state), {"issue"})
        self.assertEqual(main_state["issue"], {
            "title": "Repair graph", "project": "hana_diegetic",
            "project_about": questions.PROJECT_ABOUT["hana_diegetic"], "body": "Repair the graph.\n",
        })
        self.assertEqual(cast(questions.AreaState, payloads[1]["state"])["areas"], ["rendering", "tooling"])
        self.assertEqual(payloads[1]["state"]["issue"], main_state["issue"])
        for name in ("area", "area_reverse"):
            question = payloads[1]["questions"][name]
            assert question["type"] == "choice"
            self.assertEqual(question["criteria"]["rendering"],
                             "The work concerns rendering: Drawing scenes and managing render passes.")
            self.assertEqual(question["criteria"]["tooling"], "The work concerns the part named tooling.")
        self.assertEqual({path: path.read_bytes() for path in self.issues.glob("*.md")}, before)
        self.assertIn("Drawing scenes and managing render passes.", definitions_path.read_text())
        self.assertNotIn("fixture-key", stdout + stderr)

    def test_propose_unsure_goal_keeps_each_candidates_distinct_alignment_without_a_second_call(self) -> None:
        _ = self.issue("Vocabulary rendering")
        _ = self.issue("Vocabulary tooling", area="tooling")
        main = cast(client.Response, json.loads((FIXTURES / "proposal_unsure_goal.json").read_text()))
        area = cast(client.Response, json.loads((FIXTURES / "area_response.json").read_text()))
        with mock.patch.object(client, "urlopen", side_effect=self.proposal_replay(main, area)) as http:
            line = self.proposal()

        self.assertEqual(http.call_count, 2)
        ranking, alternatives = line.splitlines()
        parts = ranking.split(" · ")
        self.assertEqual(parts[0], "goal 1 - Ship Hana (unsure)")
        self.assertEqual(parts[1], "alignment ⭐⭐⭐⭐ (unsure)")
        self.assertIn("goal 1 - Ship Hana (alignment ⭐⭐⭐⭐ / ⭐⭐⭐ (unsure)) / "
                      + "2 - Find Collaborators (alignment ⭐⭐ / ⭐⭐⭐ (unsure)) (unsure)", alternatives)

    def test_propose_ranking_scores_only_top_values_even_when_goal_is_unsure(self) -> None:
        _ = self.issue("Vocabulary rendering")
        _ = self.issue("Vocabulary tooling", area="tooling")
        area = cast(client.Response, json.loads((FIXTURES / "area_response.json").read_text()))
        for fixture in ("main_response.json", "proposal_unsure_goal.json"):
            with self.subTest(fixture=fixture):
                main = cast(client.Response, json.loads((FIXTURES / fixture).read_text()))
                with mock.patch.object(client, "urlopen", side_effect=self.proposal_replay(main, area)):
                    output = self.proposal()
                ranking = output.splitlines()[0]
                path = self.root / "proposal.md"
                _ = path.write_text("---\nstatus: open\n---\nProposed ranking: " + ranking + "\n"
                                    + "\n".join(output.splitlines()[1:]) + "\n", encoding="utf-8")
                score = proposal_score(ProposalIssue(path, {}))
                self.assertTrue(math.isfinite(score))
                # Goal 1 bonus -2, alignment 4, impact 3, urgency 1, effort 2.
                self.assertEqual(score, 15)

    def test_propose_marks_split_effort_check_and_possible_five(self) -> None:
        _ = self.issue("Vocabulary rendering")
        _ = self.issue("Vocabulary tooling", area="tooling")
        main = cast(client.Response, json.loads((FIXTURES / "main_response.json").read_text()))
        impact = main["answers"]["impact"]
        assert impact["type"] == "score"
        impact["probabilities"] = {"0": .45, "1": 0, "2": .1, "3": 0, "4": .45}
        impact["score"], impact["confidence"] = 2, .45
        main["answers"]["deadline_named"] = {"type": "noul", "noul": .95}
        area = cast(client.Response, json.loads((FIXTURES / "area_response.json").read_text()))
        with mock.patch.object(client, "urlopen", side_effect=self.proposal_replay(main, area)):
            ranking, alternatives = self.proposal().splitlines()
            parts = ranking.split(" · ")

        self.assertEqual(parts[2], "impact ⭐ (unsure) (split)")
        self.assertIn("impact ⭐ / ⭐⭐⭐⭐⭐ (unsure) (split)", alternatives)
        self.assertEqual(parts[3], "urgency ⭐⭐⭐⭐ (unsure) (possible 5) (agent proposes)")
        self.assertIn("(check)", parts[4])

    def test_propose_reads_the_approved_jev_fields_at_call_time(self) -> None:
        _ = self.issue("Vocabulary rendering")
        _ = self.issue("Vocabulary tooling", area="tooling")
        with mock.patch.object(client, "urlopen", side_effect=self.replay), \
                mock.patch.object(questions, "JEV_FIELDS", frozenset({"category"})):
            ranking, alternatives = self.proposal().splitlines()
            parts = ranking.split(" · ")

        self.assertEqual(parts[0], "goal 1 - Ship Hana (agent proposes)")
        self.assertEqual(parts[1], "alignment ⭐⭐⭐⭐ (agent proposes)")
        self.assertEqual(parts[2], "impact ⭐⭐⭐ (agent proposes)")
        self.assertEqual(parts[5], "category feature (unsure)")
        self.assertIn("category feature / enhancement (unsure)", alternatives)

    def test_propose_normalizes_every_issue_project_shorthand_before_building_state(self) -> None:
        expected_projects = {
            "bei": "bevy_enhanced_input", "biz": "biz", "brp": "bevy_brp_mcp",
            "bwm": "bevy_window_manager", "cargo-berth": "cargo-berth", "cargo-liner": "cargo-liner",
            "cargo-mend": "cargo-mend", "cargo-port": "cargo-port", "conduit": "hana_conduit",
            "diegetic": "hana_diegetic", "fairy_dust": "fairy_dust", "hana": "hana",
            "lading": "hana_lading", "lagrange": "bevy_lagrange", "liminal": "hana_liminal",
            "nateroids": "nateroids", "panorbit": "bevy_panorbit_camera", "rust-template": "rust-template",
        }
        _ = self.issue("Vocabulary rendering")
        _ = self.issue("Vocabulary tooling", area="tooling")
        for shorthand, canonical in expected_projects.items():
            with self.subTest(project=shorthand), mock.patch.object(client, "urlopen", side_effect=self.replay) as http:
                _ = self.proposal(shorthand)
                self.assertEqual(http.call_count, 2)
                for call in http.call_args_list:
                    payload = cast(run_records.RequestPayload, json.loads(gzip.decompress(cast(bytes, call.args[0].data))))
                    self.assertEqual(payload["state"]["issue"]["project"], canonical)
                    self.assertEqual(payload["state"]["issue"]["project_about"], questions.PROJECT_ABOUT[canonical])

    def test_propose_unknown_project_exits_two_before_any_http_request(self) -> None:
        with mock.patch.object(client, "urlopen") as http:
            code, stdout, stderr = self.proposal_cli(["--project", "unknown-project", "--title", "Repair graph"])
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("unknown-project", stderr)
        http.assert_not_called()

    def test_propose_distinguishes_derived_unfiled_with_hint_from_choice_picked_unfiled(self) -> None:
        _ = self.issue("Vocabulary rendering")
        _ = self.issue("Vocabulary unfiled", area="unfiled")
        main = cast(client.Response, json.loads((FIXTURES / "main_response.json").read_text()))
        for fallback in (True, False):
            with self.subTest(fallback=fallback):
                probabilities = {"rendering": .95, "unfiled": .05} if fallback else {"rendering": .05, "unfiled": .95}
                winner = "rendering" if fallback else "unfiled"
                area: client.Response = {"model": questions.MODEL, "usage": {"input_tokens": 500, "output_tokens": 100},
                    "answers": {
                        "area": {"type": "choice", "choice": winner, "confidence": .95, "probabilities": probabilities},
                        "area_reverse": {"type": "choice", "choice": winner, "confidence": .95,
                                         "probabilities": dict(reversed(list(probabilities.items())))},
                        "area_fits": {"type": "noul", "noul": .05 if fallback else .95},
                    }}
                with mock.patch.object(client, "urlopen", side_effect=self.proposal_replay(main, area)):
                    area_text = self.proposal().splitlines()[0].split(" · ")[-1]
                expected = "area unfiled (hint: rendering) (agent proposes)" if fallback else "area unfiled (agent proposes)"
                self.assertEqual(area_text, expected)

    def test_propose_missing_or_empty_definitions_exits_two_with_path_before_http(self) -> None:
        definitions_path = self.root / "vault" / "conf" / "issue-areas.md"
        for missing in (True, False):
            with self.subTest(missing=missing):
                if missing:
                    definitions_path.unlink()
                else:
                    _ = definitions_path.write_text("No definitions yet.\n", encoding="utf-8")
                with mock.patch.object(client, "urlopen") as http:
                    code, stdout, stderr = self.proposal_cli(["--project", "diegetic", "--title", "Repair graph"])
                self.assertEqual((code, stdout), (2, ""))
                self.assertIn(str(definitions_path), stderr)
                http.assert_not_called()

    def test_propose_api_failure_in_either_request_exits_two_without_a_proposal(self) -> None:
        _ = self.issue("Vocabulary rendering")
        _ = self.issue("Vocabulary tooling", area="tooling")
        for failed_request in ("main", "area"):
            def failed_replay(request: Request, timeout: float = 60) -> FixtureHTTPResponse:
                payload = cast(run_records.RequestPayload, json.loads(gzip.decompress(cast(bytes, request.data))))
                if ("area" if "areas" in payload["state"] else "main") == failed_request:
                    raise HTTPError(request.full_url, 500, "fixture failure", Message(),
                                    io.BytesIO((FIXTURES / "http_500.json").read_bytes()))
                return self.replay(request, timeout)

            with self.subTest(failed_request=failed_request), \
                    mock.patch.object(client, "urlopen", side_effect=failed_replay) as http:
                code, stdout, stderr = self.proposal_cli(["--project", "diegetic", "--title", "Repair graph"])
            self.assertEqual((code, stdout), (2, ""))
            self.assertIn("500", stderr)
            self.assertNotIn("fixture-key", stderr)
            self.assertEqual(http.call_count, 1 if failed_request == "main" else 2)

    def test_score_persists_both_payloads_all_answers_and_decisions_without_editing_vault(self) -> None:
        target = self.issue("Target graph")
        _ = self.issue("Vocabulary seed", area="tooling")
        before = {path: path.read_bytes() for path in self.issues.glob("*.md")}
        with mock.patch.object(client, "urlopen", side_effect=self.replay) as http:
            result = self.score()
        self.assertEqual(http.call_count, 2)
        self.assertEqual(result.parent, self.state / "runs")
        records = self.records(result)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["issue_path"], str(target))
        self.assertEqual(record["question_set_version"], questions.QUESTION_SET_VERSION)
        self.assertEqual(record["model"], "jev-1.14.0")
        self.assertTrue(record["content_hash"])
        self.assertEqual(set(record["payloads"]), {"main", "area"})
        self.assertEqual(set(record["answers"]), {"main", "area"})
        main_state = record["payloads"]["main"]["state"]
        self.assertEqual(set(main_state), {"issue"})
        self.assertEqual(set(main_state["issue"]), {"title", "project", "project_about", "body"})
        self.assertEqual(main_state["issue"]["body"].strip(), "Repair the graph.")
        area_state = cast(questions.AreaState, record["payloads"]["area"]["state"])
        self.assertEqual(area_state["areas"], ["rendering", "tooling"])
        expected_main = cast(client.Response, json.loads((FIXTURES / "main_response.json").read_text()))
        expected_area = cast(client.Response, json.loads((FIXTURES / "area_response.json").read_text()))
        self.assertEqual(record["answers"]["main"], expected_main)
        self.assertEqual(record["answers"]["area"], expected_area)
        fields = record["decided_fields"]
        self.assertEqual(set(fields), {"backlog_goal", "backlog_alignment", "backlog_impact",
                                      "backlog_urgency", "backlog_effort", "category", "area"})
        self.assertEqual(cast(dict[str, object], fields["backlog_alignment"])["value"], 4)
        self.assertEqual(cast(dict[str, object], fields["backlog_impact"])["value"], 3)
        self.assertEqual(cast(dict[str, object], fields["area"])["value"], "rendering")
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertNotIn("fixture-key", result.read_text())

    def test_identical_content_version_and_model_reuse_cache_without_http(self) -> None:
        _ = self.issue("Target graph")
        _ = self.issue("Vocabulary seed", area="tooling")
        with mock.patch.object(client, "urlopen", side_effect=self.replay):
            first = self.score()
        ledger_before = (self.state / "spend.jsonl").read_bytes()
        with mock.patch.object(client, "urlopen", side_effect=AssertionError("Cache hit must not call HTTP")) as http:
            second = self.score()
        http.assert_not_called()
        self.assertNotEqual(first, second)
        self.assertEqual(self.records(first), self.records(second))
        self.assertEqual((self.state / "spend.jsonl").read_bytes(), ledger_before)

    def test_changed_area_definition_misses_cache_and_replays_both_requests(self) -> None:
        _ = self.issue("Target graph")
        _ = self.issue("Vocabulary seed", area="tooling")
        definitions = self.root / "issue-areas.md"
        first_definition = "Drawing scenes and managing render passes."
        second_definition = "Drawing scenes and managing GPU render passes."
        original = ("| area | definition | issues |\n| --- | --- | --- |\n"
                    f"| rendering | {first_definition} | 1 |\n")
        _ = definitions.write_text(original, encoding="utf-8")
        with mock.patch.object(client, "urlopen", side_effect=self.replay) as http:
            first = score_issues.score(only="Target*", issues_dir=self.issues, state_dir=self.state,
                                       area_definitions_path=definitions)
        self.assertEqual(http.call_count, 2)
        with mock.patch.object(client, "urlopen", side_effect=AssertionError("Unchanged definitions must hit cache")) as http:
            cached = score_issues.score(only="Target*", issues_dir=self.issues, state_dir=self.state,
                                        area_definitions_path=definitions)
        http.assert_not_called()
        _ = definitions.write_text(original.replace(first_definition, second_definition), encoding="utf-8")
        changed_bytes = definitions.read_bytes()
        with mock.patch.object(client, "urlopen", side_effect=self.replay) as http:
            changed = score_issues.score(only="Target*", issues_dir=self.issues, state_dir=self.state,
                                         area_definitions_path=definitions)
        self.assertEqual(http.call_count, 2)
        before = self.records(first.result_path)[0]
        after = self.records(changed.result_path)[0]
        self.assertEqual(self.records(cached.result_path), [before])
        self.assertNotEqual(before["content_hash"], after["content_hash"])
        self.assertEqual(before["answers"], after["answers"])
        self.assertEqual(before["payloads"]["main"], after["payloads"]["main"])
        for entry, definition in ((before, first_definition), (after, second_definition)):
            for name in ("area", "area_reverse"):
                question = entry["payloads"]["area"]["questions"][name]
                assert question["type"] == "choice"
                self.assertEqual(question["criteria"]["rendering"], f"The work concerns rendering: {definition}")
                self.assertEqual(question["criteria"]["tooling"], "The work concerns the part named tooling.")
        self.assertEqual(definitions.read_bytes(), changed_bytes)

    def test_experiment_area_requests_use_definitions_in_production_and_variant_questions(self) -> None:
        _ = self.issue("Target A")
        _ = self.issue("Vocabulary seed", area="tooling")
        definitions = self.root / "issue-areas.md"
        _ = definitions.write_text(
            "| area | definition | issues |\n| --- | --- | --- |\n"
            + "| rendering | Drawing scenes and managing render passes. | 1 |\n", encoding="utf-8")
        replacement = questions.area_questions(["rendering", "tooling"], {})["area_fits"]
        replacement["instructions"] = "Does `issue` concern an area in `areas`?"

        def replay_variant(request: Request, timeout: float = 60) -> FixtureHTTPResponse:
            payload = cast(run_records.RequestPayload, json.loads(gzip.decompress(cast(bytes, request.data))))
            if "areas" not in payload["state"]:
                return self.replay(request, timeout)
            response = cast(client.Response, json.loads((FIXTURES / "area_response.json").read_text()))
            response["answers"]["area_fits__fit_scope"] = {"type": "noul", "noul": .95}
            return FixtureHTTPResponse(json.dumps(response).encode())

        with mock.patch.object(client, "urlopen", side_effect=replay_variant) as http:
            runs = score_issues.experiment({"fit_scope": {"area_fits": replacement}}, only="Target A",
                                           issues_dir=self.issues, state_dir=self.state,
                                           area_definitions_path=definitions)
        self.assertEqual(http.call_count, 2)
        self.assertEqual(set(runs), {"production", "fit_scope"})
        for run in runs.values():
            entry = self.records(run.result_path)[0]
            for name in ("area", "area_reverse"):
                question = entry["payloads"]["area"]["questions"][name]
                assert question["type"] == "choice"
                self.assertEqual(question["criteria"]["rendering"],
                                 "The work concerns rendering: Drawing scenes and managing render passes.")
            self.assertIn("area_fits__fit_scope", entry["payloads"]["area"]["questions"])

    def test_cache_reuses_raw_answers_but_refreshes_stale_certainty_under_current_policy(self) -> None:
        _ = self.issue("Target graph")
        _ = self.issue("Vocabulary seed", area="tooling")
        with mock.patch.object(client, "urlopen", side_effect=self.replay):
            cached_path = self.score()
        cached = self.records(cached_path)[0]
        current_decisions = cached["decided_fields"]
        stale_decisions = dict(current_decisions)
        for field in ("area", "backlog_urgency", "backlog_effort"):
            stale_decisions[field] = dict(cast(dict[str, object], current_decisions[field]), certainty="sure")
        cached["decided_fields"] = stale_decisions
        _ = cached_path.write_text(json.dumps(cached) + "\n", encoding="utf-8")
        cached_bytes = cached_path.read_bytes()
        ledger_before = (self.state / "spend.jsonl").read_bytes()

        with mock.patch.object(client, "urlopen", side_effect=AssertionError("Cache hit must not call HTTP")) as http:
            refreshed_path = self.score()

        http.assert_not_called()
        refreshed = self.records(refreshed_path)[0]
        self.assertEqual(refreshed["answers"], cached["answers"])
        self.assertEqual(refreshed["payloads"], cached["payloads"])
        self.assertEqual(refreshed["decided_fields"], current_decisions)
        for field in ("area", "backlog_urgency", "backlog_effort"):
            with self.subTest(field=field):
                self.assertEqual(cast(dict[str, object], refreshed["decided_fields"][field])["certainty"], "unsure")
        self.assertEqual(cached_path.read_bytes(), cached_bytes)
        self.assertEqual((self.state / "spend.jsonl").read_bytes(), ledger_before)

    def test_tune_cli_never_sends_a_holdout_issue(self) -> None:
        tune = self.issue("Target A")  # SHA1(filename) % 10 == 1.
        _ = self.issue("Target G", area="tooling")  # Residue 8: holdout.
        requested_titles: list[str] = []
        original_score = score_issues.score

        def isolated_score(only: str = "*", limit: int = 0, *,
                           split: Literal["tune", "holdout", "all"] = "all",
                           fresh: bool = False) -> run_records.ScoreRun:
            return original_score(only, limit, split=split, fresh=fresh,
                                  issues_dir=self.issues, state_dir=self.state)

        def observed_replay(request: Request, timeout: float = 60) -> FixtureHTTPResponse:
            payload = cast(dict[str, object], json.loads(gzip.decompress(cast(bytes, request.data))))
            state = cast(questions.IssueState, payload["state"])
            requested_titles.append(state["issue"]["title"])
            return self.replay(request, timeout)

        stdout = io.StringIO()
        with mock.patch.object(score_issues, "score", side_effect=isolated_score), \
                mock.patch.object(client, "urlopen", side_effect=observed_replay), \
                contextlib.redirect_stdout(stdout):
            code = score_issues.main(["score", "--split", "tune"])
        self.assertEqual(code, 0)
        self.assertEqual(requested_titles, [tune.stem, tune.stem])
        self.assertEqual([row["issue_path"] for row in self.records(Path(stdout.getvalue().strip()))], [str(tune)])

    def test_holdout_selection_precedes_the_limit(self) -> None:
        _ = self.issue("Target A")
        holdout = self.issue("Target G", area="tooling")
        _ = self.issue("Target K")
        with mock.patch.object(client, "urlopen", side_effect=self.replay) as http:
            run = score_issues.score("Target*", 1, split="holdout",
                                     issues_dir=self.issues, state_dir=self.state)
        self.assertEqual(http.call_count, 2)
        self.assertEqual([row["issue_path"] for row in self.records(run.result_path)], [str(holdout)])

    def test_fresh_cli_reads_new_answers_and_persists_them_for_later_cache_hits(self) -> None:
        _ = self.issue("Target graph")
        _ = self.issue("Vocabulary seed", area="tooling")
        with mock.patch.object(client, "urlopen", side_effect=self.replay):
            first = self.score()
        original_score = score_issues.score

        def isolated_score(only: str = "*", limit: int = 0, *,
                           split: Literal["tune", "holdout", "all"] = "all",
                           fresh: bool = False) -> run_records.ScoreRun:
            return original_score(only, limit, split=split, fresh=fresh,
                                  issues_dir=self.issues, state_dir=self.state)

        def new_answer(request: Request, timeout: float = 60) -> FixtureHTTPResponse:
            raw = cast(client.Response, json.loads(self.replay(request, timeout).read()))
            if "impact" in raw["answers"]:
                answer = raw["answers"]["impact"]
                if answer["type"] == "score":
                    answer["probabilities"] = {"0": 0., "1": 0., "2": 0., "3": 0., "4": 1.}
                    answer["score"], answer["confidence"] = 4., 1.
            return FixtureHTTPResponse(json.dumps(raw).encode())

        stdout = io.StringIO()
        with mock.patch.object(score_issues, "score", side_effect=isolated_score), \
                mock.patch.object(client, "urlopen", side_effect=new_answer) as http, \
                contextlib.redirect_stdout(stdout):
            code = score_issues.main(["score", "--only", "Target*", "--fresh"])
        self.assertEqual(code, 0)
        self.assertEqual(http.call_count, 2)
        fresh_rows = self.records(Path(stdout.getvalue().strip()))
        self.assertEqual(cast(dict[str, object], self.records(first)[0]["decided_fields"]["backlog_impact"])["value"], 3)
        self.assertEqual(cast(dict[str, object], fresh_rows[0]["decided_fields"]["backlog_impact"])["value"], 5)
        with mock.patch.object(client, "urlopen", side_effect=AssertionError("Fresh result must be cached")):
            cached = self.score()
        self.assertEqual(self.records(cached), fresh_rows)

    def test_changed_body_question_version_and_model_each_invalidate_cache(self) -> None:
        target = self.issue("Target graph")
        _ = self.issue("Vocabulary seed", area="tooling")
        with mock.patch.object(client, "urlopen", side_effect=self.replay) as http:
            first = self.score()
            _ = target.write_text(target.read_text() + "A new user-visible requirement.\n")
            changed = self.score()
            self.assertEqual(http.call_count, 4)
            self.assertNotEqual(self.records(first)[0]["content_hash"], self.records(changed)[0]["content_hash"])
            with mock.patch.object(questions, "QUESTION_SET_VERSION", "fixture-next-version"):
                versioned = self.score()
            self.assertEqual(http.call_count, 6)
            self.assertEqual(self.records(versioned)[0]["question_set_version"], "fixture-next-version")
            _ = self.score(model="jev-other-version")
            self.assertEqual(http.call_count, 8)

    def test_changed_area_vocabulary_invalidates_cached_area_request(self) -> None:
        _ = self.issue("Target graph")
        seed = self.issue("Vocabulary seed", area="tooling")
        with mock.patch.object(client, "urlopen", side_effect=self.replay) as http:
            _ = self.score()
            _ = seed.write_text(seed.read_text().replace("[[tooling]]", "[[layout]]"))

            def changed_area_replay(request: Request, timeout: float = 60) -> FixtureHTTPResponse:
                response = self.replay(request, timeout)
                return FixtureHTTPResponse(response.read().replace(b'"tooling"', b'"layout"'))

            http.side_effect = changed_area_replay
            updated = self.score()
        self.assertEqual(http.call_count, 4)
        state = cast(questions.AreaState, self.records(updated)[0]["payloads"]["area"]["state"])
        self.assertEqual(state["areas"], ["layout", "rendering"])

    def test_scoring_runs_eight_issues_concurrently_and_writes_each_once(self) -> None:
        for number in range(10):
            _ = self.issue(f"Target {number}", area="rendering" if number % 2 else "tooling")
        released = threading.Event()
        lock = threading.Lock()
        active = 0
        peak = 0

        def concurrent_replay(state: Mapping[str, object], request_questions: Mapping[str, questions.Question],
                              model: str, *, ledger_path: Path) -> client.Response:
            nonlocal active, peak
            _ = model, ledger_path
            with lock:
                active += 1
                peak = max(peak, active)
                if active == 8:
                    released.set()
            try:
                if not released.wait(timeout=TEST_HANG_GUARD_SECS):
                    raise AssertionError("Eight issues never reached the request boundary together")
                fixture = "area_response.json" if "areas" in state else "main_response.json"
                response = cast(object, json.loads((FIXTURES / fixture).read_text()))
                return client.parse_response(response, request_questions)
            finally:
                with lock:
                    active -= 1

        with mock.patch.object(client, "system_one", side_effect=concurrent_replay) as system_one:
            result = self.score()
        self.assertEqual(peak, 8)
        self.assertEqual(system_one.call_count, 20)
        records = self.records(result)
        self.assertEqual(len(records), 10)
        self.assertEqual(len({record["issue_path"] for record in records}), 10)

    def test_cli_glob_and_limit_score_only_matching_open_non_style_issues(self) -> None:
        first = self.issue("Target A")
        second = self.issue("Target B", area="tooling")
        _ = self.issue("Other issue")
        _ = self.issue("Target style", extra='style_rule: "global:rust/names.md"\n')
        closed = self.issue("Target closed")
        _ = closed.write_text(closed.read_text().replace("status: open", "status: closed"))
        original_score = score_issues.score

        def isolated_score(only: str = "*", limit: int = 0, *,
                           split: Literal["tune", "holdout", "all"] = "all",
                           fresh: bool = False) -> run_records.ScoreRun:
            return original_score(only, limit, split=split, fresh=fresh,
                                  issues_dir=self.issues, state_dir=self.state)

        with mock.patch.object(score_issues, "score", side_effect=isolated_score), \
                mock.patch.object(client, "urlopen", side_effect=self.replay) as http, \
                contextlib.redirect_stdout(io.StringIO()):
            code = score_issues.main(["score", "--only", "Target*", "--limit", "1"])
        self.assertEqual(code, 0)
        self.assertEqual(http.call_count, 2)
        records = [record for path in (self.state / "runs").glob("*.jsonl") for record in self.records(path)]
        self.assertEqual(len(records), 1)
        self.assertIn(records[0]["issue_path"], {str(first), str(second)})

    def test_failed_issue_preserves_other_results_and_only_success_is_cached(self) -> None:
        failed = self.issue("Target A failure")
        success = self.issue("Target B success")
        _ = self.issue("Vocabulary seed", area="tooling")
        failed_calls: list[str] = []

        def partial_replay(request: Request, timeout: float = 60) -> FixtureHTTPResponse:
            payload = cast(dict[str, object], json.loads(gzip.decompress(cast(bytes, request.data))))
            state = cast(questions.IssueState, payload["state"])
            if state["issue"]["title"] == failed.stem:
                failed_calls.append(failed.stem)
                raise client.TypeSafeError("fixture API failure")
            return self.replay(request, timeout)

        original_score = score_issues.score

        def isolated_score(only: str = "*", limit: int = 0, *,
                           split: Literal["tune", "holdout", "all"] = "all",
                           fresh: bool = False) -> run_records.ScoreRun:
            return original_score(only, limit, split=split, fresh=fresh,
                                  issues_dir=self.issues, state_dir=self.state)

        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(score_issues, "score", side_effect=isolated_score), \
                mock.patch.object(client, "urlopen", side_effect=partial_replay), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = score_issues.main(["score", "--only", "Target*"])
        self.assertEqual(code, 1)
        run_path = Path(stdout.getvalue().strip())
        records = [cast(dict[str, object], json.loads(line)) for line in run_path.read_text().splitlines()]
        self.assertEqual(len(records), 2)
        self.assertIn({"issue_path": str(failed), "error": "fixture API failure"}, records)
        self.assertEqual({record["issue_path"] for record in records}, {str(failed), str(success)})
        cache = score_issues.load_cache(self.state)
        self.assertEqual({key[0] for key in cache}, {str(success)})
        self.assertIn("1 issue failed", stderr.getvalue())

        with mock.patch.object(client, "urlopen", side_effect=partial_replay) as http:
            retried = original_score("Target*", issues_dir=self.issues, state_dir=self.state)
        self.assertEqual(retried.failure_count, 1)
        self.assertEqual(http.call_count, 1)
        self.assertEqual(failed_calls, [failed.stem, failed.stem])
        self.assertEqual(len(retried.result_path.read_text().splitlines()), 2)

    def test_completed_result_is_flushed_before_an_earlier_issue_finishes(self) -> None:
        earlier = self.issue("Target A slow failure")
        completed = self.issue("Target B success")
        _ = self.issue("Vocabulary seed", area="tooling")
        persisted = threading.Event()
        original_open = Path.open

        def observed_open(path: Path, mode: str = "r", buffering: int = -1,
                          encoding: str | None = None, errors: str | None = None,
                          newline: str | None = None) -> TextIO:
            handle = cast(TextIO, original_open(path, mode, buffering, encoding, errors, newline))
            if mode == "x" and path.parent == self.state / "runs":
                flush = handle.flush

                def observe_flush() -> None:
                    flush()
                    records = [cast(dict[str, object], json.loads(line))
                               for line in path.read_text().splitlines()]
                    if any(record["issue_path"] == str(completed) for record in records):
                        persisted.set()

                _ = self.enterContext(mock.patch.object(handle, "flush", side_effect=observe_flush))
            return handle

        def delayed_failure(request: Request, timeout: float = 60) -> FixtureHTTPResponse:
            payload = cast(dict[str, object], json.loads(gzip.decompress(cast(bytes, request.data))))
            state = cast(questions.IssueState, payload["state"])
            if state["issue"]["title"] == earlier.stem:
                if not persisted.wait(timeout=TEST_HANG_GUARD_SECS):
                    raise AssertionError("Completed issue was not flushed while an earlier issue was running")
                raise client.TypeSafeError("fixture delayed failure")
            return self.replay(request, timeout)

        with mock.patch.object(Path, "open", autospec=True, side_effect=observed_open), \
                mock.patch.object(client, "urlopen", side_effect=delayed_failure):
            run = score_issues.score("Target*", issues_dir=self.issues, state_dir=self.state)
        records = [cast(dict[str, object], json.loads(line)) for line in run.result_path.read_text().splitlines()]
        self.assertEqual(records[0]["issue_path"], str(completed))
        self.assertEqual(records[1], {"issue_path": str(earlier), "error": "fixture delayed failure"})
        self.assertEqual(run.failure_count, 1)

    def test_budget_stop_keeps_in_flight_success_and_blocks_the_next_area_request(self) -> None:
        exhausted = self.issue("Target A budget")
        unfinished = self.issue("Target B main")
        completed = self.issue("Target C area")
        _ = self.issue("Vocabulary seed", area="tooling")
        stopped = threading.Event()
        main_started = threading.Event()
        area_started = threading.Event()
        requests: list[tuple[str, bool]] = []

        def budget_replay(state: Mapping[str, object], request_questions: Mapping[str, questions.Question],
                          model: str, *, ledger_path: Path) -> client.Response:
            _ = model, ledger_path
            title = cast(questions.IssueDescription, state["issue"])["title"]
            area_request = "areas" in state
            requests.append((title, area_request))
            if title == exhausted.stem:
                if not main_started.wait(timeout=TEST_HANG_GUARD_SECS) or not area_started.wait(timeout=TEST_HANG_GUARD_SECS):
                    raise AssertionError("Other requests did not start")
                raise client.BudgetExceeded("fixture budget exhausted")
            if title == unfinished.stem:
                main_started.set()
                if not stopped.wait(timeout=TEST_HANG_GUARD_SECS):
                    raise AssertionError("Budget stop did not reach in-flight work")
            elif area_request:
                area_started.set()
                if not stopped.wait(timeout=TEST_HANG_GUARD_SECS):
                    raise AssertionError("Budget stop did not reach in-flight work")
            fixture = "area_response.json" if area_request else "main_response.json"
            return client.parse_response(cast(object, json.loads((FIXTURES / fixture).read_text())), request_questions)

        with mock.patch.object(score_issues, "Event", return_value=stopped), \
                mock.patch.object(client, "system_one", side_effect=budget_replay):
            run = score_issues.score("Target*", issues_dir=self.issues, state_dir=self.state)
        self.assertEqual(run.failure_count, 2)
        self.assertNotIn((unfinished.stem, True), requests)
        self.assertEqual(len(requests), 4)
        records = [cast(dict[str, object], json.loads(line)) for line in run.result_path.read_text().splitlines()]
        self.assertEqual(len(records), 3)
        self.assertEqual({record["issue_path"] for record in records if "answers" in record}, {str(completed)})

    def test_budget_exhaustion_stops_new_requests_and_preserves_cached_results(self) -> None:
        cached = self.issue("Target B cached")
        _ = self.issue("Vocabulary seed", area="tooling")
        for number in range(10):
            _ = self.issue(f"Target A {number}")
        with mock.patch.object(client, "urlopen", side_effect=self.replay):
            _ = self.score(only="Target B*")
        with mock.patch.object(client, "system_one", side_effect=client.BudgetExceeded("budget exhausted")) as api:
            run = score_issues.score("Target*", issues_dir=self.issues, state_dir=self.state)
        self.assertGreater(run.failure_count, 0)
        self.assertEqual(api.call_count, 1)
        records = [cast(dict[str, object], json.loads(line)) for line in run.result_path.read_text().splitlines()]
        self.assertLess(len(records), 11)
        # A completed prior result remains reusable even after budget exhaustion.
        self.assertEqual({key[0] for key in score_issues.load_cache(self.state)}, {str(cached)})


if __name__ == "__main__":
    _ = unittest.main()
