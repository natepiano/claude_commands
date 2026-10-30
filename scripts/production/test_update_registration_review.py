"""Registration behavior against a private seeded fixture."""

from __future__ import annotations

import io
from contextlib import redirect_stdout
import json
import os
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast
from unittest import mock

from scripts.production.update_registration_test_support import RegistrationFixture, registration, showrunners, dailies_render, read_production, review
import showrunner_tree

from scripts.production.update_registration_test_support import setUpModule as setUpModule


class RegistrationTests(RegistrationFixture):
    def test_new_object_question_requires_full_text_but_log_remains_a_parser(self) -> None:
        state = self.question_state([{"text": "Choose layout"}])
        parsed = registration.judgment_state(state, "log")
        self.assertIsInstance(parsed.outstanding, registration.ReplaceOutstanding)
        with self.assertRaisesRegex(registration.RegistrationFailure, "a new question needs its full text"):
            _ = self.run_questions(state)
        self.assertFalse(self.outstanding().exists())
        self.assertFalse((self.state / "notifier/showrunner-example/pending_reviewed.json").exists())

    def test_held_question_batch_writes_no_records_and_rerun_accepts_reviewed_text(self) -> None:
        outstanding = self.outstanding()
        _ = outstanding.write_text('[{"since":"2026-10-01T08:00","text":"Existing reminder"}]')
        history = outstanding.with_suffix(".history.jsonl")
        _ = history.write_text(json.dumps({"time": datetime.now(timezone.utc).isoformat(), "text": "Earlier"}) + "\n")
        pending = self.state / "notifier/showrunner-example/pending_reviewed.json"
        _ = self.question_instance()
        _ = pending.write_text(json.dumps({"items": [{"kind": "reviewed", "text": "Earlier", "reviewed": "Earlier question?"}]}))
        before = [path.read_bytes() for path in (outstanding, history, pending)]
        full = "Build 42 failed.\nRetry (recommended) or stop?"
        state = self.question_state([{"text": "Retry build", "question": full},
                                     {"text": "Choose compiler", "question": "Use stable (recommended) or nightly?"}])
        with mock.patch.object(review, "review_question", side_effect=[
                review.QuestionAccepted(full),
                review.QuestionAwaitingFacts("Use stable (recommended) or nightly?", ("Which target?",))]) as reviewer:
            with self.assertRaisesRegex(registration.RegistrationFailure, "question held for review: Choose compiler: Which target"):
                _ = self.run_questions(state)
        self.assertEqual([path.read_bytes() for path in (outstanding, history, pending)], before)
        self.assertEqual(reviewer.call_args_list[0].args, (review.QuestionKey("example", "Retry build"), full))
        accepted = "Build 42 failed on natedev.\nRetry (recommended) or stop?"
        with mock.patch.object(review, "review_question", side_effect=[
                review.QuestionAccepted(accepted), review.QuestionAccepted("Use stable (recommended) or nightly?")]):
            output = self.run_questions(state)
        self.assertIn("reviewed question: " + accepted, output)
        rows = cast(list[dict[str, str]], json.loads(outstanding.read_text()))
        self.assertEqual(rows[0]["question"], full)
        self.assertEqual(rows[0]["reviewed"], accepted)
        lines = [cast(dict[str, str], json.loads(line)) for line in history.read_text().splitlines()]
        self.assertEqual(lines[1]["reviewed"], accepted)

    def test_question_rewrite_recheck_failure_keeps_registration_files_unchanged_and_revision(self) -> None:
        outstanding = self.outstanding()
        _ = outstanding.write_text("[]")
        history = outstanding.with_suffix(".history.jsonl")
        _ = history.write_text("")
        pending = self.state / "notifier/showrunner-example/pending_reviewed.json"
        pending.parent.mkdir(parents=True)
        _ = pending.write_text('{"items":[]}')
        paths = (outstanding, history, pending)
        before = [path.read_bytes() for path in paths]
        original, rewritten = "Retry it?", "Retry build 42 (recommended) or stop?"
        state = self.question_state([{"text": "Retry build", "question": original}])
        responses = iter([
            review.VerdictRecord(verdict="rewrite", title="", action=None, body=rewritten, questions=[],
                failed_rules=[{"rule": "reader-starts-cold", "quote": original}]),
            review.VerdictRecord(verdict="ask", title="", action=None, body=rewritten, questions=["Which machine?"],
                failed_rules=[{"rule": "reader-starts-cold", "quote": "build 42"}]),
            review.VerdictRecord(verdict="send", title="", action=None, body=original, questions=[], failed_rules=[])])
        calls: list[tuple[list[str], float, dict[str, str]]] = []
        def runner(argv: list[str], *, timeout: float,
                   env: dict[str, str]) -> subprocess.CompletedProcess[str]:
            calls.append((argv.copy(), timeout, env.copy()))
            if argv[0] == "zsh":
                return subprocess.CompletedProcess(argv, 0, "Question rules", "")
            if argv[0] == "bash":
                return subprocess.CompletedProcess(argv, 0, "agent=sonnet effort=high", "")
            self.assertEqual(Path(argv[0]).name, "claude")
            return subprocess.CompletedProcess(argv, 0, json.dumps(next(responses)), "")
        real_review = review.review_question
        def question_review(key: review.QuestionKey, question: str) -> review.QuestionOutcome:
            return real_review(key, question, runner=runner)
        with (mock.patch.dict(os.environ, {"NOTIFY_STATE_DIR": str(self.state / "notify")}),
              mock.patch.object(review, "GUIDE", self.root),
              mock.patch.object(review, "reviewer_environment", return_value=({}, "claude2")),
              mock.patch.object(review, "reviewer_binary", return_value=review.ResolvedReviewerBinary(self.root / "claude")),
              mock.patch.object(review, "review_question", side_effect=question_review)):
            with self.assertRaisesRegex(registration.RegistrationFailure, "Which machine"):
                _ = self.run_questions(state)
            self.assertEqual([path.read_bytes() for path in paths], before)
            self.assertTrue((self.state / "notify/questions" / (review.QuestionKey("example", "Retry build").digest + ".json")).exists())
            self.assertIn("reviewed question: " + original, self.run_questions(state))
        prompts = [call[0][-1] for call in calls if Path(call[0][0]).name == "claude"]
        self.assertIn("Revision: true", prompts[-1])
        self.assertEqual(len(prompts), 3)
        self.assertTrue(all(0 < call[1] <= review.review_timeout_seconds() for call in calls))

    def test_later_registration_keeps_reviewed_and_unavailable_questions_and_recall_is_multiline(self) -> None:
        _ = self.question_instance()
        full = "Build 42 failed.\nRetry (recommended) or stop?"
        unavailable = "Use stable (recommended) or nightly?"
        state = self.question_state([{"text": "Retry build", "question": full}])
        with mock.patch.object(review, "review_question", return_value=review.QuestionAccepted(full)):
            _ = self.run_questions(state)
        state = self.question_state(["Retry build", {"text": "Choose compiler", "question": unavailable}])
        with mock.patch.object(review, "review_question", return_value=review.QuestionReviewUnavailable(unavailable, "timeout")):
            _ = self.run_questions(state)
        state = self.question_state([{"text": "Retry build", "after": "2099-01-01T08:00"}, "Choose compiler", "Reminder"])
        with mock.patch.object(review, "review_question", side_effect=AssertionError("reminder reviewed")):
            _ = self.run_questions(state)
        rows = cast(list[dict[str, str]], json.loads(self.outstanding().read_text()))
        self.assertEqual(rows[0]["reviewed"], full)
        self.assertEqual(rows[0]["question"], full)
        self.assertEqual(rows[1]["question"], unavailable)
        self.assertEqual(rows[1]["review_unavailable"], "timeout")
        self.assertNotIn("reviewed", rows[1])
        with (mock.patch.dict(os.environ, self.env),
              mock.patch.object(showrunner_tree, "user_facing", return_value=["example"]),
              redirect_stdout(io.StringIO()) as output):
            registration.open_items(read_production(self.doc))
        self.assertIn(full, output.getvalue())
        self.assertIn(unavailable, output.getvalue())
        self.assertIn("Reminder", output.getvalue())
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=1)
        with mock.patch.dict(os.environ, self.env):
            self.assertIn(" ".join(full.split()), review.registered_questions(cutoff))
            _ = self.run_questions(self.question_state([]))
            self.assertIn(" ".join(full.split()), review.registered_questions(cutoff))

    def test_blank_reviewer_rewrite_stores_original_as_review_unavailable(self) -> None:
        question = "Retry build 42?"
        state = self.question_state([{"text": "Retry build", "question": question}])
        real_review = review.review_question
        for body in ("", " \n\t"):
            with self.subTest(body=body):
                self.outstanding().unlink(missing_ok=True)
                decisions: list[review.VerdictRecord] = [
                    review.VerdictRecord(verdict="rewrite", title="", action=None, body=body, questions=[],
                        failed_rules=[{"rule": "reader-starts-cold", "quote": question}]),
                    review.VerdictRecord(verdict="send", title="", action=None, body=body,
                        questions=[], failed_rules=[])]
                calls: list[tuple[list[str], float, dict[str, str]]] = []
                def runner(argv: list[str], *, timeout: float,
                           env: dict[str, str]) -> subprocess.CompletedProcess[str]:
                    calls.append((argv.copy(), timeout, env.copy()))
                    if argv[0] == "zsh":
                        return subprocess.CompletedProcess(argv, 0, "Question rules", "")
                    if argv[0] == "bash":
                        return subprocess.CompletedProcess(argv, 0, "agent=sonnet effort=high", "")
                    self.assertEqual(Path(argv[0]).name, "claude")
                    return subprocess.CompletedProcess(argv, 0, json.dumps(decisions.pop(0)), "")
                def question_review(key: review.QuestionKey, text: str) -> review.QuestionOutcome:
                    return real_review(key, text, runner=runner)
                with (mock.patch.dict(os.environ, {"NOTIFY_STATE_DIR": str(self.state / "notify")}),
                      mock.patch.object(review, "GUIDE", self.root),
                      mock.patch.object(review, "reviewer_environment", return_value=({}, "claude2")),
                      mock.patch.object(review, "reviewer_binary", return_value=review.ResolvedReviewerBinary(self.root / "claude")),
                      mock.patch.object(review, "review_question", side_effect=question_review)):
                    output = self.run_questions(state)
                self.assertEqual(len([call for call in calls if Path(call[0][0]).name == "claude"]), 1)
                rows = dailies_render.read_outstanding(self.outstanding())
                self.assertEqual(len(rows), 1)
                self.assertIsInstance(rows[0], dailies_render.ReviewUnavailableOutstandingQuestion)
                row = rows[0]
                assert isinstance(row, dailies_render.ReviewUnavailableOutstandingQuestion)
                self.assertEqual(row.question, question)
                self.assertIn("rewrite needs nonempty text", row.reason)
                self.assertIn("reviewed question: " + question, output)

    def test_reviewed_question_without_instance_does_not_create_a_timer_directory(self) -> None:
        question = "Retry build 42?"
        state = self.question_state([{"text": "Retry build", "question": question}])
        with mock.patch.object(review, "review_question", return_value=review.QuestionAccepted(question)):
            output = self.run_questions(state)
        self.assertIn("reviewed question: " + question, output)
        rows = dailies_render.read_outstanding(self.outstanding())
        self.assertIsInstance(rows[0], dailies_render.ReviewedOutstandingQuestion)
        self.assertFalse((self.state / "notifier/showrunner-example").exists())
        self.assertEqual(showrunners.registered_showrunners(self.state / "notifier"), [])

    def test_duplicate_question_text_is_reviewed_and_recorded_once_using_first_occurrence(self) -> None:
        _ = self.question_instance()
        first = "Retry build 42?"
        state = self.question_state([
            {"text": "Retry build", "question": first},
            {"text": "Retry build", "question": "Use build 43?", "after": "2099-01-01T08:00"},
            "Reminder", "Reminder"])
        with mock.patch.object(review, "review_question", return_value=review.QuestionAccepted(first)) as reviewer:
            _ = self.run_questions(state)
        reviewer.assert_called_once_with(review.QuestionKey("example", "Retry build"), first)
        rows = cast(list[dict[str, str]], json.loads(self.outstanding().read_text()))
        self.assertEqual([row["text"] for row in rows], ["Retry build", "Reminder"])
        self.assertEqual(rows[0]["question"], first)
        self.assertNotIn("after", rows[0])
        history = [cast(dict[str, str], json.loads(line))
                   for line in self.outstanding().with_suffix(".history.jsonl").read_text().splitlines()]
        self.assertEqual([row["text"] for row in history], ["Retry build", "Reminder"])


if __name__ == "__main__":
    _ = unittest.main()
