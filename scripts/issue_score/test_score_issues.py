"""Replay the score pipeline and cache without touching the user's vault or state."""
from __future__ import annotations

import contextlib
import gzip
import io
import json
import os
import tempfile
import threading
import unittest
from collections.abc import Mapping
from email.message import Message
from pathlib import Path
from typing import Literal, TextIO, cast, override
from unittest import mock
from urllib.request import Request

from scripts.issue_score import client, questions, score_issues
from scripts.issue_score import records as run_records

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
