"""Reviewer and held-push contracts, with local state and fake processes/posts."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict, cast, final, override
from unittest import mock
from zoneinfo import ZoneInfo

import pushover
import review
import reply_asks
import push_texts
from user_action import ActionRequired, NoActionRequired, SessionLine, first_line

REAL_REVIEW = review.review

if TYPE_CHECKING:
    from ..accounts import accounts
else:
    from scripts.accounts import accounts


class VerdictJSON(TypedDict):
    verdict: str
    title: str
    action: str | None
    body: str
    questions: list[str]
    failed_rules: list[review.RuleFailureRecord]


class ReviewerEnvelope(TypedDict):
    structured_output: VerdictJSON
    is_error: bool


class TranscriptMessage(TypedDict):
    id: str
    role: str
    stop_reason: str
    content: list[dict[str, str]]


class TranscriptEntry(TypedDict):
    type: str
    timestamp: str
    message: TranscriptMessage


@dataclass(frozen=True)
class ProcessCall:
    argv: list[str]
    timeout: float
    environment: dict[str, str]


@final
class FakeReviewer:
    def __init__(self, value: VerdictJSON, *,
                 failure: Literal["none", "timeout", "missing", "exit", "json", "quota"] = "none",
                 guide_ok: bool = True, assignment_ok: bool = True) -> None:
        self.value = value
        self.failure = failure
        self.guide_ok = guide_ok
        self.assignment_ok = assignment_ok
        self.calls: list[ProcessCall] = []

    def __call__(self, argv: list[str], *, timeout: float,
                 env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(ProcessCall(argv.copy(), timeout, env.copy()))
        if argv[0] == "zsh":
            return subprocess.CompletedProcess(argv, 0 if self.guide_ok else 1,
                                               "Loaded push rules" if self.guide_ok else "", "")
        if argv[0] == "bash":
            return subprocess.CompletedProcess(argv, 0 if self.assignment_ok else 1,
                                               "task=message_review.reviewer agent=sonnet effort=high", "")
        if Path(argv[0]).name != "claude":
            raise AssertionError(f"unexpected process: {argv}")
        if self.failure == "timeout":
            raise subprocess.TimeoutExpired(argv, timeout)
        if self.failure == "missing":
            raise FileNotFoundError("claude unavailable")
        if self.failure == "exit":
            return subprocess.CompletedProcess(argv, 1, "", "reviewer failed")
        if self.failure == "json":
            return subprocess.CompletedProcess(argv, 0, "unreadable JSON", "")
        envelope: ReviewerEnvelope = {"structured_output": self.value, "is_error": self.failure == "quota"}
        return subprocess.CompletedProcess(argv, 0, json.dumps(envelope), "")


def with_footer(message: str, sender: str, template: str = "none (free text)") -> str:
    """A posted push: the message, then its "Sent by" and "Template" lines last."""
    return f"{message}\n\nSent by: {sender}\nTemplate: {template}"


def verdict(draft: review.Draft, decision: str = "send", /, **changes: object) -> VerdictJSON:
    value: VerdictJSON = {
        "verdict": decision, "title": draft.title,
        "action": draft.action.text if isinstance(draft.action, ActionRequired) else None,
        "body": draft.body, "questions": [], "failed_rules": ([] if decision == "send" else
            [{"rule": "reader-starts-cold", "quote": draft.body[:10]}]),
    }
    return cast(VerdictJSON, cast(object, dict(value) | changes))


@final
class LocalReviewTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.draft = review.Draft("writer one", "1", "Build needs a decision",
                                  ActionRequired("Choose in the natedev build terminal."),
                                  "The build stopped. Choose whether to retry.", True)

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        notes = self.root / "notes"
        notes.mkdir()
        (self.root / ".claude").mkdir()
        _ = (self.root / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "reviewer@example.invalid"}}))
        _ = (notes / "claude2.md").write_text("---\nlogin: reviewer@example.invalid\n---\n")
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.root), "AGENT_NOTES_DIR": str(notes), "NOTIFY_STATE_DIR": str(self.root / "state")})))
        self.assertEqual(accounts.setup("claude2"), [])
        _ = (accounts.account_dir("claude2") / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "reviewer@example.invalid"}}))
        guide = self.root / "guide"
        guide.mkdir()
        _ = self.enterContext(mock.patch.object(review, "GUIDE", guide))
        _ = self.enterContext(mock.patch.object(accounts, "role_account", return_value=accounts.NamedAccount("claude2", accounts.account_dir("claude2"))))
        # Calls without an explicit fake must never start a reviewer or notifier.
        _ = self.enterContext(mock.patch.object(subprocess, "run", side_effect=AssertionError("real process")))
        _ = self.enterContext(mock.patch.object(review, "schedule"))
        _ = self.enterContext(mock.patch.object(pushover, "http_post", side_effect=AssertionError("real post")))
        _ = self.enterContext(mock.patch.object(pushover, "LOG_FILE", self.root / "pushes.jsonl"))
        config = self.root / "notify.conf"
        _ = config.write_text("REVIEW_TIMEOUT_SECONDS=60\n")
        _ = self.enterContext(mock.patch.object(review, "NOTIFY_CONFIG", config))
        binary = self.root / "bin/claude"
        binary.parent.mkdir()
        _ = binary.write_text("fake reviewer\n")
        binary.chmod(0o700)
        _ = self.enterContext(mock.patch.object(shutil, "which", return_value=str(binary)))

    def test_questions_keep_multiline_headers_and_long_text_without_push_limits(self) -> None:
        for prefix in ("Session: Owner", "Action: choose one", "No action needed."):
            question = prefix + "\n" + "Question context. " * 80 + "\nChoose retry (recommended) or stop?"
            draft = review.Draft("demo", "0", "", NoActionRequired(), question, False)
            runner = FakeReviewer(verdict(draft))
            key = review.QuestionKey("demo", prefix)
            with (mock.patch.object(review, "length_failures", side_effect=AssertionError("push count")),
                  mock.patch.object(review, "split_session", side_effect=AssertionError("push header"))):
                self.assertEqual(review.review_question(key, question, runner=runner),
                                 review.QuestionAccepted(question))
            self.assertIn(question, runner.calls[-1].argv[-1])
            self.assertIn("question,all", runner.calls[0].argv)
        self.assertEqual([item["kind"] for item in self.entries()], ["question"] * 3)
        self.assertFalse(pushover.LOG_FILE.exists())

    def test_question_holds_revise_independently_by_summary_and_never_expire_to_post(self) -> None:
        first = review.QuestionKey("demo", "Retry build")
        second = review.QuestionKey("demo", "Choose compiler")
        question = "Retry the build?"
        draft = review.Draft("demo", "0", "", NoActionRequired(), question, False)
        asking = FakeReviewer(verdict(draft, "ask", questions=["Which build?"]))
        for key in (first, second):
            self.assertIsInstance(review.review_question(key, question, runner=asking), review.QuestionAwaitingFacts)
        reworded = "Retry build 42 (recommended) or stop?"
        revised_draft = replace(draft, body=reworded)
        unresolved = FakeReviewer(verdict(revised_draft, "ask", questions=["Which machine?"]))
        self.assertIsInstance(review.review_question(first, reworded, runner=unresolved), review.QuestionAwaitingFacts)
        self.assertIn("Revision: true", unresolved.calls[-1].argv[-1])
        passed = FakeReviewer(verdict(revised_draft))
        self.assertEqual(review.review_question(first, reworded, runner=passed), review.QuestionAccepted(reworded))
        paths = list((review.state_path() / "questions").glob("*.json"))
        self.assertEqual([path.stem for path in paths], [second.digest])
        with mock.patch.object(pushover, "http_post", side_effect=AssertionError("question posted")) as post:
            self.assertEqual(review.due(post=post), 0)
        post.assert_not_called()
        self.assertFalse((review.state_path() / "held").exists())

    def test_question_rewrite_needs_approval_and_recheck_failures_stay_held(self) -> None:
        key = review.QuestionKey("demo", "Retry")
        draft = review.Draft("demo", "0", "", NoActionRequired(), "Retry it?", False)
        rewritten = replace(draft, body="Retry build 42 (recommended) or stop?")
        for decision in ("ask", "rewrite", "send", "timeout"):
            with self.subTest(recheck=decision):
                first = FakeReviewer(verdict(draft, "rewrite", body=rewritten.body))
                second = FakeReviewer(verdict(rewritten, decision if decision != "timeout" else "send",
                                              questions=["Which machine?"] if decision == "ask" else []),
                                      failure="timeout" if decision == "timeout" else "none")
                calls = 0
                def runner(argv: list[str], *, timeout: float,
                           env: dict[str, str]) -> subprocess.CompletedProcess[str]:
                    nonlocal calls
                    if Path(argv[0]).name == "claude":
                        calls += 1
                    return (second if calls > 1 else first)(argv, timeout=timeout, env=env)
                outcome = review.review_question(key, draft.body, runner=runner)
                self.assertEqual(calls, 2)
                if decision == "send":
                    self.assertEqual(outcome, review.QuestionAccepted(rewritten.body))
                else:
                    self.assertIsInstance(outcome, (review.QuestionAwaitingFacts, review.QuestionReviewFailed))
                    self.assertTrue((review.state_path() / "questions" / (key.digest + ".json")).exists())

    def test_unavailable_question_review_preserves_original_and_logs_reason(self) -> None:
        draft = review.Draft("demo", "0", "", NoActionRequired(), "Retry build 42?", False)
        runner = FakeReviewer(verdict(draft), failure="timeout")
        self.assertEqual(review.review_question(review.QuestionKey("demo", "Retry"), draft.body, runner=runner),
                         review.QuestionReviewUnavailable(draft.body, "timeout"))
        record = self.entries()[0]
        self.assertEqual(record["kind"], "question")
        self.assertEqual(record["verdict"], "unreviewed: timeout")

    def test_content_reviews_reject_blank_rewrites(self) -> None:
        draft = review.Draft("demo", "0", "", NoActionRequired(), "Retry build 42?", False)
        for body in ("", " \n\t"):
            for mode in ("reply", "question"):
                with self.subTest(body=body, mode=mode):
                    outcome = review.validate(verdict(draft, "rewrite", body=body), draft, False,
                                              reply=mode == "reply", question=mode == "question")
                    self.assertIsInstance(outcome, review.Unreviewed)
                    assert isinstance(outcome, review.Unreviewed)
                    self.assertIn("rewrite needs nonempty text", outcome.why)

    def test_blank_question_input_is_rejected_before_review(self) -> None:
        runner = mock.Mock(side_effect=AssertionError("blank question reached reviewer"))
        with self.assertRaisesRegex(ValueError, "question needs nonempty text"):
            _ = review.review_question(review.QuestionKey("demo", "Retry"), " \n\t", runner=runner)
        runner.assert_not_called()

    def test_session_survives_review_hold_records_and_repeated_headers(self) -> None:
        draft = replace(self.draft, session=SessionLine("Owner"))
        record = review.message_record(draft)
        self.assertEqual(record.get("session"), "Owner")
        value = verdict(draft, body=first_line(draft.action, draft.session) + "\n" + draft.body)
        self.assertEqual(review.validate(value, draft, False), review.Approved(draft))
        held = review.Held(draft.source, time.time(), draft, draft, ["Stop Owner?"])
        review.hold(held)
        self.assertEqual(review.held_for(draft.source), review.LiveHold(held))
        runner = FakeReviewer(verdict(draft))
        self.assertIsInstance(review.review(draft, False, runner=runner), review.Approved)
        self.assertIn("Session: Owner\nAction: ", runner.calls[-1].argv[-1])

    def test_session_line_counts_toward_review_body_limit(self) -> None:
        draft = replace(self.draft, action=ActionRequired("stop"), body="x" * 480)
        self.assertEqual(review.length_failures(draft), [])
        self.assertEqual(review.length_failures(replace(draft, session=SessionLine("Owner"))), ["push-body-under-500"])

    def test_direct_check_keeps_session_above_action_in_review_and_record(self) -> None:
        draft = review.Draft("review.check", "1", "Owner is blocked", ActionRequired("stop Owner"),
                             "Reason: waiting.", False, SessionLine("Owner"))
        message = first_line(draft.action, draft.session) + "\n" + draft.body
        runner = FakeReviewer(verdict(draft, body=message))

        def check(current: review.Draft, revision: bool) -> review.ReviewOutcome:
            self.assertEqual(current, draft)
            outcome = REAL_REVIEW(current, revision, runner=runner)
            self.assertIsInstance(outcome, review.Approved)
            return outcome

        with mock.patch.object(review, "review", side_effect=check), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(review.main(["check", "--title", draft.title, "--action", "stop Owner",
                                          "--priority", "1", "--text", "Session: Owner\n" + draft.body]), 0)
        self.assertIn(message, output.getvalue())
        self.assertIn("Session: Owner\nAction: stop Owner", runner.calls[-1].argv[-1])
        self.assertEqual(self.entries()[0]["draft"], review.message_record(draft))

    def test_replay_preserves_session_above_action(self) -> None:
        message = "Session: Owner\nAction: stop Owner\nReason: waiting."
        pushover.log("shutdown", "1", "Owner is blocked", message, "sent")
        def approve(draft: review.Draft, _revision: bool) -> review.ReviewOutcome:
            return review.Approved(draft)

        with mock.patch.object(review, "review", side_effect=approve) as reviewer, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(review.replay(), 0)
        draft = cast(review.Draft, reviewer.call_args.args[0])
        self.assertEqual(draft.session, SessionLine("Owner"))
        self.assertEqual(draft.action, ActionRequired("stop Owner"))
        self.assertEqual(draft.body, "Reason: waiting.")
        self.assertIn(message, output.getvalue())
        self.assertEqual(cast(dict[str, object], self.entries()[0]["draft"])["session"], "Owner")

    def test_reviewer_without_claude_on_path_uses_an_executable_fallback_and_logs_it(self) -> None:
        binary = self.root / ".local/bin/claude"
        binary.parent.mkdir(parents=True)
        _ = binary.write_text("fake reviewer\n")
        binary.chmod(0o700)
        runner = FakeReviewer(verdict(self.draft))
        with (mock.patch.object(shutil, "which", return_value=None) as which,
              mock.patch.object(Path, "home", return_value=self.root)):
            outcome = review.review(self.draft, False, runner=runner)
        which.assert_called_once_with("claude")
        self.assertIsInstance(outcome, review.Approved)
        command = next(call.argv for call in runner.calls if Path(call.argv[0]).name == "claude")
        self.assertEqual(command[0], str(binary))
        review.log_review(self.draft, outcome)
        self.assertEqual(self.entries()[0]["binary"], str(binary))

    def test_missing_reviewer_reports_paths_tried_without_starting_a_process(self) -> None:
        binary = self.root / ".local/bin/claude"
        binary.parent.mkdir(parents=True)
        _ = binary.write_text("not executable\n")
        binary.chmod(0o600)
        runner = FakeReviewer(verdict(self.draft))
        with (mock.patch.object(shutil, "which", return_value=None) as which,
              mock.patch.object(Path, "home", return_value=self.root),
              mock.patch.dict(os.environ, {"USER": "review-test"}),
              mock.patch.object(os, "access", return_value=False)):
            outcome = review.review(self.draft, False, runner=runner)
        which.assert_called_once_with("claude")
        self.assertIsInstance(outcome, review.Unreviewed)
        assert isinstance(outcome, review.Unreviewed)
        self.assertEqual(outcome.why, "no claude binary: PATH: claude, " + str(binary)
                         + ", /etc/profiles/per-user/review-test/bin/claude, /run/current-system/sw/bin/claude")
        self.assertEqual(runner.calls, [])
        review.log_review(self.draft, outcome)
        self.assertEqual(self.entries()[0]["verdict"], "unreviewed: " + outcome.why)

    def test_reviewer_uses_the_first_executable_fallback_in_order(self) -> None:
        candidates = (self.root / ".local/bin/claude",
                      Path("/etc/profiles/per-user/review-test/bin/claude"),
                      Path("/run/current-system/sw/bin/claude"))
        for index, expected in enumerate(candidates):
            def executable(path: Path, _mode: int) -> bool:
                return path in candidates[index:]
            with (self.subTest(expected=expected),
                  mock.patch.object(shutil, "which", return_value=None),
                  mock.patch.object(Path, "home", return_value=self.root),
                  mock.patch.dict(os.environ, {"USER": "review-test"}),
                  mock.patch.object(Path, "is_file", return_value=True),
                  mock.patch.object(os, "access", side_effect=executable) as access):
                self.assertEqual(review.reviewer_binary(), review.ResolvedReviewerBinary(expected))
                self.assertEqual([call.args[0] for call in access.call_args_list], list(candidates[:index + 1]))

    def test_review_config_accepts_positive_finite_seconds_and_logs_fallbacks(self) -> None:
        for contents, expected in (("# local limit\nREVIEW_TIMEOUT_SECONDS=90 # seconds\n", 90),
                                   ("REVIEW_TIMEOUT_SECONDS=0.5\n", 0.5)):
            _ = review.NOTIFY_CONFIG.write_text(contents)
            with redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(review.review_timeout_seconds(), expected)
            self.assertEqual(errors.getvalue(), "")
        for contents, reason in (("OTHER=90\n", "missing REVIEW_TIMEOUT_SECONDS"),
                                 ("REVIEW_TIMEOUT_SECONDS=oops", "could not convert"),
                                 ("REVIEW_TIMEOUT_SECONDS=0", "positive and finite"),
                                 ("REVIEW_TIMEOUT_SECONDS=-1", "positive and finite"),
                                 ("REVIEW_TIMEOUT_SECONDS=nan", "positive and finite"),
                                 ("REVIEW_TIMEOUT_SECONDS=inf", "positive and finite")):
            _ = review.NOTIFY_CONFIG.write_text(contents)
            with redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(review.review_timeout_seconds(), 60)
            self.assertIn(reason, errors.getvalue())
            self.assertIn("using REVIEW_TIMEOUT_SECONDS=60", errors.getvalue())
        review.NOTIFY_CONFIG.unlink()
        with redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(review.review_timeout_seconds(), 60)
        self.assertIn("No such file", errors.getvalue())

    def test_all_delivery_waits_derive_from_the_configured_review_and_post_limits(self) -> None:
        _ = review.NOTIFY_CONFIG.write_text("REVIEW_TIMEOUT_SECONDS=90\n")
        self.assertEqual(review.revision_seconds(), 195)
        self.assertEqual(review.user_send_seconds(), 205)
        self.assertEqual(review.claim_stale_seconds(), 205)
        self.assertEqual(review.posting_wait_seconds(), 110)
        with mock.patch.object(pushover, "TIMEOUT_S", 30):
            self.assertEqual(review.revision_seconds(), 210)
            self.assertEqual(review.user_send_seconds(), 220)
            self.assertEqual(review.claim_stale_seconds(), 220)
            self.assertEqual(review.posting_wait_seconds(), 110)
        _ = review.NOTIFY_CONFIG.write_text("REVIEW_TIMEOUT_SECONDS=60\n")
        self.assertEqual(review.user_send_seconds(), 145)
        self.assertEqual(review.posting_wait_seconds(), 110)

    def test_stale_claim_recovery_respects_a_longer_configured_revision_budget(self) -> None:
        _ = review.NOTIFY_CONFIG.write_text("REVIEW_TIMEOUT_SECONDS=150\n")
        held = self.held()
        review.hold(held)
        with mock.patch.object(time, "time", return_value=1301):
            self.assertTrue(review.claim(held.source))
        with mock.patch.object(time, "time", return_value=1301 + 324):
            self.assertFalse(review.claim(held.source))
            self.assertEqual(review.release_if_matches(held.source, review.hold_identity(held)),
                             review.ReleaseOutcome.POSTING)
            self.assertEqual(review.due(post=mock.Mock(side_effect=AssertionError("early post")),
                                        keys_file=self.root / "unused"), 0)
        with mock.patch.object(time, "time", return_value=1301 + 325):
            self.assertEqual(review.release_if_matches(held.source, review.hold_identity(held)),
                             review.ReleaseOutcome.RELEASED)

    def reviewed(self, value: VerdictJSON, *, revision: bool = False) -> review.ReviewOutcome:
        return review._review(self.draft, revision, FakeReviewer(value), timeout_seconds=review.review_timeout_seconds())  # pyright: ignore[reportPrivateUsage]

    def entries(self) -> list[dict[str, object]]:
        path = self.root / "state/review.jsonl"
        return [cast(dict[str, object], json.loads(line)) for line in path.read_text().splitlines()]

    def transcript(self, name: str, replies: list[tuple[str, str, str]]) -> Path:
        path = self.root / name
        records = [TranscriptEntry(type="assistant", timestamp=stamp,
                   message=TranscriptMessage(id=f"message-{index}", role="assistant", stop_reason=stop,
                       content=[{"type": "text", "text": text}]))
                   for index, (stamp, text, stop) in enumerate(replies)]
        _ = path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
        return path

    def test_every_review_prompt_names_the_user_zone_and_its_date(self) -> None:
        reply = review.ReplyContext(self.root / "session.jsonl", datetime(2026, 10, 25, 12, tzinfo=timezone.utc), "")
        question = review.QuestionReview(review.QuestionKey("demo", "Retry"))
        contexts: tuple[review.PushReview | review.ReplyContext | review.QuestionReview, ...] = (
            review.PUSH_REVIEW, reply, question)
        for zone in ("America/Curacao", "America/New_York"):
            now = datetime.now(ZoneInfo(zone))
            line = f"User zone: {now.tzname()} ({zone}). Today: {now:%Y-%m-%d}."
            for context in contexts:
                with self.subTest(zone=zone, context=type(context).__name__), mock.patch.dict(os.environ, {"USER_ZONE": zone}):
                    runner = FakeReviewer(verdict(self.draft))
                    _ = review._review(self.draft, False, runner, context, timeout_seconds=60)  # pyright: ignore[reportPrivateUsage]
                    command = runner.calls[-1].argv
                    self.assertEqual(command[-1].splitlines()[0], line)
                    self.assertIn("Times are in the user zone the draft gives.",
                                  command[command.index("--system-prompt") + 1])
        self.assertEqual(datetime.now(ZoneInfo("America/Curacao")).tzname(), "AST")

    def test_sample_uses_reply_rules_date_and_previous_reply_without_phone_limits(self) -> None:
        old = "The prior decision is still open."
        body = "The build completed. " * 80
        path = self.transcript("session.jsonl", [
            ("2026-10-11T10:00:00Z", old, "end_turn"),
            ("2026-10-13T10:00:00Z", "I will run a tool.", "tool_use"),
            ("2026-10-13T11:00:00Z", body.strip(), "end_turn")])
        draft = review.Draft("review.sample", "0", "", NoActionRequired(), body.strip(), False)
        runner = FakeReviewer(verdict(draft))
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(review.sample([path], since=datetime.fromisoformat("2026-10-12T00:00:00+00:00"), runner=runner), 0)
        guide = runner.calls[0].argv
        self.assertEqual(guide[guide.index("--tags") + 1], "reply,all")
        prompt = runner.calls[-1].argv[-1]
        self.assertIn("Reply date: 2026-10-13T11:00:00+00:00", prompt)
        self.assertIn("Previous reply:\n" + old, prompt)
        self.assertNotIn("I will run a tool.", prompt)
        self.assertIn("passed: 1; failed: 0; unreviewed: 0", output.getvalue())
        entry = self.entries()[0]
        self.assertIs(entry["sample"], True)
        self.assertEqual(entry["kind"], "reply")
        self.assertEqual(entry["previous_reply"], old)
        self.assertEqual(entry["transcript"], str(path))
        self.assertIsNone(entry["sent"])
        self.assertFalse((self.root / "state/held").exists())
        self.assertFalse(pushover.LOG_FILE.exists())

    def test_sample_limit_selects_recent_non_question_replies_across_transcripts(self) -> None:
        paths = [self.transcript("one.jsonl", [
            ("2026-10-13T10:00:00Z", "The first build completed.", "end_turn"),
            ("2026-10-13T12:00:00Z", "Would you retry the build?", "end_turn")]),
            self.transcript("two.jsonl", [("2026-10-13T11:00:00Z", "The second build completed.", "end_turn")])]
        draft = review.Draft("review.sample", "0", "", NoActionRequired(), "The second build completed.", False)
        runner = FakeReviewer(verdict(draft))
        with (mock.patch.dict(os.environ, {"SHOWRUNNER_STATE_DIR": str(self.root / "showrunner")}),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(review.sample(paths, 1, since=datetime.fromisoformat("2026-10-12T00:00:00+00:00"), runner=runner), 0)
        self.assertIn("sample: 1 replies", output.getvalue())
        self.assertIn("replies with unregistered questions: 1", output.getvalue())
        self.assertEqual(len(self.entries()), 1)
        self.assertEqual(self.entries()[0]["transcript"], str(paths[1]))

    def test_sample_reports_unreviewed_apart_from_valid_rule_failures(self) -> None:
        path = self.transcript("session.jsonl", [
            ("2026-10-13T10:00:00Z", "The first build completed.", "end_turn"),
            ("2026-10-13T11:00:00Z", "The second build completed.", "end_turn")])
        def runner(argv: list[str], *, timeout: float, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
            body = "The second build completed." if "second build" in argv[-1] else "The first build completed."
            draft = review.Draft("review.sample", "0", "", NoActionRequired(), body, False)
            fake = FakeReviewer(verdict(draft, "rewrite", body="The build passed.",
                failed_rules=[{"rule": "reader-starts-cold", "quote": body}]),
                failure="timeout" if "second build" in argv[-1] else "none")
            return fake(argv, timeout=timeout, env=env)
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(review.sample([path], since=datetime.fromisoformat("2026-10-12T00:00:00+00:00"), runner=runner), 0)
        self.assertIn("passed: 0; failed: 1; unreviewed: 1", output.getvalue())
        self.assertIn("sample failed rules: reader-starts-cold=1", output.getvalue())

    def test_sample_registered_questions_and_quoted_examples_are_not_unregistered(self) -> None:
        path = self.transcript("session.jsonl", [
            ("2026-10-13T10:00:00Z", "Would you retry the build?", "end_turn"),
            ("2026-10-13T11:00:00Z", "> Example: retry?\n```\nquery?\n```\nThe build passed.", "end_turn")])
        registration = self.root / "showrunner/outstanding/build.json"
        registration.parent.mkdir(parents=True)
        _ = registration.write_text(json.dumps([{"text": "Would you retry the build?", "since": "2026-10-13T10:00"}]))
        body = "> Example: retry?\n```\nquery?\n```\nThe build passed."
        runner = FakeReviewer(verdict(review.Draft("review.sample", "0", "", NoActionRequired(), body, False)))
        with (mock.patch.dict(os.environ, {"SHOWRUNNER_STATE_DIR": str(registration.parents[1])}),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(review.sample([path], since=datetime.fromisoformat("2026-10-12T00:00:00+00:00"), runner=runner), 0)
        self.assertIn("replies with unregistered questions: 0", output.getvalue())

    def test_sample_cli_accepts_paths_limit_and_zoned_since(self) -> None:
        with mock.patch.object(review, "sample", return_value=0) as sample:
            self.assertEqual(review.main(["sample", "one.jsonl", "two.jsonl", "--limit", "2",
                                          "--since", "2026-10-12T00:00:00Z"]), 0)
        self.assertEqual(sample.call_args.args, ([Path("one.jsonl"), Path("two.jsonl")], 2))
        self.assertEqual(sample.call_args.kwargs["since"], datetime.fromisoformat("2026-10-12T00:00:00+00:00"))

    def test_sample_still_counts_a_relayed_question_as_a_detected_ask(self) -> None:
        # Recipient filtering belongs to the Stop check, not to the sample count.
        body = ('**Also sent to the hana showrunner:** distant picking is listed as "waiting for review". '
                + 'Did you approve it? It stays "waiting for review" until hana answers.')
        path = self.transcript("relay.jsonl", [("2026-10-15T21:49:10Z", body, "end_turn")])
        runner = mock.Mock(side_effect=AssertionError("relayed ask reached the reviewer"))
        with (mock.patch.dict(os.environ, {"SHOWRUNNER_STATE_DIR": str(self.root / "showrunner")}),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(review.sample([path], 0, since=datetime.fromisoformat("2026-10-15T00:00:00Z"),
                                           runner=runner), 0)
        self.assertEqual(reply_asks.reply_questions(body).sentences, ("Did you approve it?",))
        self.assertIn("sample: 0 replies", output.getvalue())
        self.assertIn("replies with unregistered questions: 1", output.getvalue())
        runner.assert_not_called()

    def test_request_sentences_and_recommended_choices_are_asks(self) -> None:
        requests = ["Reply upgrade or wait.", "Choose upgrade or wait.", "Pick a version.",
                    "Confirm the version.", "Decide whether to upgrade.", "Approve the upgrade.",
                    "Tell me which version.", "Let me know which version.", "Say upgrade or wait.",
                    "Please choose a version."]
        for request in requests:
            with self.subTest(request=request):
                self.assertEqual(reply_asks.reply_questions("The check passed. " + request), reply_asks.ReplyAsks((request,), False))
        self.assertEqual(reply_asks.reply_questions("Upgrade (recommended): use the new compiler."),
                         reply_asks.ReplyAsks((), True, ("Upgrade (recommended): use the new compiler.",)))
        self.assertEqual(reply_asks.reply_questions("- Choose a version.\n2. Confirm the version."),
                         reply_asks.ReplyAsks(("Choose a version.", "Confirm the version."), False))
        self.assertEqual(reply_asks.reply_questions(
            "> Reply upgrade or wait.\n```\nChoose a version.\n```\n~~~\nApprove it.\n~~~\n"
            + "The example is `Tell me which version.` and ``Choose a version.``."), reply_asks.ReplyAsks((), False))

    def test_choice_lines_include_questions_but_skip_quotes_and_code(self) -> None:
        line = "Retry (recommended) or stop?"
        asks = reply_asks.reply_questions(line + "\n> Use stable (recommended).\n"
            + "```\nUse nightly (recommended).\n```\nExample: `Upgrade (recommended)`")
        self.assertEqual(asks.choice_lines, (line,))
        self.assertIn(line, asks.sentences)

    def test_detected_choice_lines_keep_inline_code_for_record_matching(self) -> None:
        line = "Run `retry build-42` (recommended), or stop?"
        asks = reply_asks.reply_questions(line + "\nExample: `Stop (recommended)`")
        self.assertEqual(asks.choice_lines, (line,))

    def test_user_asks_exclude_explicit_peer_recipient_headings_and_lead_ins(self) -> None:
        relays = (
            '**Also sent to the hana showrunner:** distant picking is listed as "waiting for review". '
            + 'Did you approve it? It stays "waiting for review" until hana answers.',
            "**To the hana showrunner:**\nDid you approve it?",
            "I asked the handler-enh unit director: Should we start phase 3?",
        )
        for relay in relays:
            with self.subTest(relay=relay):
                self.assertEqual(reply_asks.user_reply_questions(relay), reply_asks.ReplyAsks((), False))

    def test_attributed_unit_choices_are_exempt_without_changing_measurement_choices(self) -> None:
        choice = '"Run phases 1 and 2 (recommended), or wait."'
        reply = "The handler-enh unit director said, in its own words:\n\n" + choice
        self.assertEqual(reply_asks.reply_questions(reply).choice_lines, (choice,))
        self.assertEqual(reply_asks.user_reply_questions(reply), reply_asks.ReplyAsks((), False))

    def test_naming_a_unit_as_the_subject_or_executor_keeps_the_user_ask(self) -> None:
        # Real user decisions that name a unit as subject or executor.
        question = "Should handler-enh go ahead with phases 1 and 2?"
        request = "Say go and I'll have cargo-liner do it."
        choice = ("Allow (recommended, since both requests are a day old): "
                  + "the shutdown unit runs the cancel once, then moves dashboard-unit to claude 4.")
        self.assertEqual(reply_asks.user_reply_questions(question + "\n" + request + "\n" + choice),
                         reply_asks.ReplyAsks((question, request), True, (choice,)))

    def test_peer_recipient_scope_ends_before_the_next_user_paragraph(self) -> None:
        relay = "**Also sent to the hana showrunner:** Did you approve it?"
        question = "Want me to stop the nine idle ones now?"
        choice = "Leave on (recommended), or turn off."
        for separator in ("\n\n", "\n\n**Question for you:**\n"):
            with self.subTest(separator=separator):
                self.assertEqual(reply_asks.user_reply_questions(relay + separator + question + "\n" + choice),
                                 reply_asks.ReplyAsks((question,), True, (choice,)))

    def test_quoted_or_code_recipient_headings_do_not_hide_a_user_request(self) -> None:
        request = "Say go and I'll run it."
        prefixes = (
            "`To the hana showrunner:`\n\n",
            "```text\nTo the handler-enh unit director:\nChoose retry (recommended).\n```\n\n",
            "**To the hana showrunner:**\n> Did you approve it?\n\n",
        )
        for prefix in prefixes:
            with self.subTest(prefix=prefix):
                self.assertEqual(reply_asks.user_reply_questions(prefix + request), reply_asks.ReplyAsks((request,), False))

    def test_a_peer_message_in_code_does_not_extend_recipient_scope_to_the_user(self) -> None:
        request = "Want me to have it added?"
        reply = ("**To the hana showrunner:**\n\n"
                 + "```text\nDid you approve it?\n```\n\n" + request)
        self.assertEqual(reply_asks.user_reply_questions(reply), reply_asks.ReplyAsks((request,), False))

    def test_purpose_headings_keep_user_requests_and_choices(self) -> None:
        request = "Say go and I'll cancel the two requests."
        choice = "Cancel (recommended), or wait."
        for heading in ("To unblock the shutdown unit:", "To finish the worker move:",
                        "For review by the hana showrunner:"):
            with self.subTest(heading=heading):
                self.assertEqual(reply_asks.user_reply_questions(heading + "\n" + request + "\n" + choice),
                                 reply_asks.ReplyAsks((request,), True, (choice,)))

    def test_inline_return_to_user_retains_the_user_question_and_inline_code(self) -> None:
        question = "should I retire `handler-enh` (recommended), or wait?"
        for marker in ("Back to you:", "For you:", "Question for you:", "To you:", "Your decision:"):
            with self.subTest(marker=marker):
                reply = "**To the hana showrunner:** Did you approve it? " + marker + " " + question
                self.assertEqual(reply_asks.user_reply_questions(reply), reply_asks.reply_questions(question))

    def test_recipient_headings_without_colons_exempt_only_their_next_paragraph(self) -> None:
        user_question = "Want me to stop it?"
        for heading in ("**To the hana showrunner**", "# For the handler-enh unit director",
                        "To the hana showrunner"):
            for separator in ("\n", "\n\n"):
                with self.subTest(heading=heading, separator=separator):
                    reply = heading + separator + "Did you approve it?\n\n" + user_question
                    self.assertEqual(reply_asks.user_reply_questions(reply), reply_asks.ReplyAsks((user_question,), False))

    def test_subject_headings_keep_the_question_addressed_to_the_user(self) -> None:
        cases = (
            ("**Hana session**\nWant me to restart it?", "Want me to restart it?"),
            ("### Shutdown unit\nShould I cancel the two old requests?", "Should I cancel the two old requests?"),
            ("Trunk unit: remote control is off. Want me to turn it on?", "Want me to turn it on?"),
            ("hana showrunner\nDid you approve it?", "Did you approve it?"),
        )
        for reply, question in cases:
            with self.subTest(reply=reply):
                self.assertEqual(reply_asks.user_reply_questions(reply), reply_asks.ReplyAsks((question,), False))

    def test_inline_code_recipient_and_return_markers_do_not_change_the_addressee(self) -> None:
        question = "Want me to use it?"
        reply = "`To the hana showrunner:` is only an example. " + question
        self.assertEqual(reply_asks.user_reply_questions(reply), reply_asks.ReplyAsks((question,), False))
        reply = "**To the hana showrunner:** The example says `Back to you:`. Did you approve it?"
        self.assertEqual(reply_asks.user_reply_questions(reply), reply_asks.ReplyAsks((), False))

    def test_session_address_colon_keeps_the_following_paragraph_addressed_to_the_peer(self) -> None:
        reply = "To session:other:\n\nDid you approve it?\n\nWant me to stop it?"
        self.assertEqual(reply_asks.user_reply_questions(reply), reply_asks.ReplyAsks(("Want me to stop it?",), False))

    def test_reviewed_question_is_registered_from_outstanding_and_after_closing(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=1)
        directory = self.root / "showrunner/outstanding"
        directory.mkdir(parents=True)
        accepted = "Build 42 failed.\nRetry (recommended) or stop?"
        item = {"since": "2026-10-15T10:00", "text": "Retry build",
                "question": "Retry it?", "reviewed": accepted}
        current = directory / "demo.json"
        _ = current.write_text(json.dumps([item]))
        with mock.patch.dict(os.environ, {"SHOWRUNNER_STATE_DIR": str(directory.parent)}):
            self.assertIn(" ".join(accepted.split()), review.registered_questions(cutoff))
            _ = current.write_text("[]")
            history = review.QuestionRegistration(time=datetime.now(timezone.utc).isoformat(),
                text=item["text"], question=item["question"], reviewed=accepted)
            _ = current.with_suffix(".history.jsonl").write_text(json.dumps(history) + "\n")
            self.assertIn(" ".join(accepted.split()), review.registered_questions(cutoff))

    def test_sample_matches_registered_asks_without_matching_their_choice_lines(self) -> None:
        question = "Would you retry the build?"
        registered_text = "Retry the build"
        choices = "Yes (recommended): retry the build.\nNo: keep the current build."
        registration = self.root / "showrunner/outstanding/build.json"
        registration.parent.mkdir(parents=True)
        _ = registration.write_text(json.dumps([{"text": question}, {"text": registered_text}]))
        cases = [(question + "\n" + choices, 0),
                 ("Would you change the compiler?\n" + choices, 1),
                 (registered_text + ".\n" + choices, 0),
                 (choices, 1)]
        runner = mock.Mock(side_effect=AssertionError("choice reply reached the reviewer"))
        for index, (body, expected) in enumerate(cases):
            with self.subTest(body=body):
                path = self.transcript(f"choices-{index}.jsonl", [("2026-10-13T10:00:00Z", body, "end_turn")])
                with (mock.patch.dict(os.environ, {"SHOWRUNNER_STATE_DIR": str(registration.parents[1])}),
                      redirect_stdout(io.StringIO()) as output):
                    self.assertEqual(review.sample([path], since=datetime.fromisoformat("2026-10-12T00:00:00+00:00"),
                                                   runner=runner), 0)
                self.assertIn("sample: 0 replies", output.getvalue())
                self.assertIn(f"replies with unregistered questions: {expected}", output.getvalue())
        runner.assert_not_called()

    def test_sample_recognises_reviewed_inline_code_before_and_after_closing(self) -> None:
        cutoff = datetime.fromisoformat("2026-10-12T00:00:00+00:00")
        question = "Run `check.py` (recommended), or wait?"
        path = self.transcript("inline-code.jsonl", [
            ("2026-10-13T10:00:00Z", question, "end_turn"),
            ("2026-10-13T11:00:00Z", "The build passed.", "end_turn")])
        registration = self.root / "showrunner/outstanding/build.json"
        registration.parent.mkdir(parents=True)
        item = {"text": "Run check", "question": question, "reviewed": question}
        _ = registration.write_text(json.dumps([item]))
        draft = review.Draft("review.sample", "0", "", NoActionRequired(), "The build passed.", False)
        for closed in (False, True):
            with self.subTest(closed=closed):
                if closed:
                    _ = registration.write_text("[]")
                    _ = registration.with_suffix(".history.jsonl").write_text(
                        json.dumps(dict(item, time="2026-10-13T10:00:00+00:00")) + "\n")
                runner = FakeReviewer(verdict(draft))
                with (mock.patch.dict(os.environ, {"SHOWRUNNER_STATE_DIR": str(registration.parents[1])}),
                      redirect_stdout(io.StringIO()) as output):
                    self.assertEqual(review.sample([path], since=cutoff, runner=runner), 0)
                self.assertIn("sample: 1 replies; passed: 1", output.getvalue())
                self.assertIn("replies with unregistered questions: 0", output.getvalue())

    def test_sample_excludes_imperative_requests_from_replies_that_ask_nothing(self) -> None:
        path = self.transcript("requests.jsonl", [
            ("2026-10-13T10:00:00Z", "Reply upgrade or wait.", "end_turn"),
            ("2026-10-13T11:00:00Z", "The check passed. Choose upgrade or wait.", "end_turn"),
            ("2026-10-13T12:00:00Z", "Upgrade (recommended): use the new compiler.", "end_turn")])
        runner = mock.Mock(side_effect=AssertionError("asking reply reached the reviewer"))
        with (mock.patch.dict(os.environ, {"SHOWRUNNER_STATE_DIR": str(self.root / "showrunner")}),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(review.sample([path], since=datetime.fromisoformat("2026-10-12T00:00:00+00:00"),
                                           runner=runner), 0)
        runner.assert_not_called()
        self.assertIn("sample: 0 replies", output.getvalue())
        self.assertIn("replies with unregistered questions: 3", output.getvalue())

    def test_sample_matches_each_question_sentence_on_the_same_line(self) -> None:
        first, second = "Would you retry the build?", "Which version should it use?"
        self.assertEqual(reply_asks.reply_questions(first + " " + second), reply_asks.ReplyAsks((first, second), False))
        path = self.transcript("questions.jsonl", [("2026-10-13T10:00:00Z", first + " " + second, "end_turn")])
        registration = self.root / "showrunner/outstanding/build.json"
        registration.parent.mkdir(parents=True)
        _ = registration.write_text(json.dumps([{"text": first, "since": "2026-10-13T10:00"}]))
        with (mock.patch.dict(os.environ, {"SHOWRUNNER_STATE_DIR": str(registration.parents[1])}),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(review.sample([path], 0, since=datetime.fromisoformat("2026-10-12T00:00:00+00:00")), 0)
        self.assertIn("replies with unregistered questions: 1", output.getvalue())

    def test_sample_counts_a_reply_with_two_unregistered_questions_once(self) -> None:
        path = self.transcript("questions.jsonl", [("2026-10-13T10:00:00Z",
            "Would you retry the build?\nWhich version should it use?", "end_turn")])
        with (mock.patch.dict(os.environ, {"SHOWRUNNER_STATE_DIR": str(self.root / "showrunner")}),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(review.sample([path], 0, since=datetime.fromisoformat("2026-10-12T00:00:00+00:00")), 0)
        self.assertIn("replies with unregistered questions: 1", output.getvalue())

    def test_sample_remembers_a_registered_question_after_it_closes(self) -> None:
        question = "Would you retry the build?"
        path = self.transcript("closed.jsonl", [("2026-10-13T10:00:00Z", question, "end_turn")])
        registration = self.root / "showrunner/outstanding/build.json"
        registration.parent.mkdir(parents=True)
        _ = registration.write_text("[]\n")
        _ = registration.with_suffix(".history.jsonl").write_text(json.dumps(review.QuestionRegistration(
            time="2026-10-13T10:00:00+00:00", text=question)) + "\n")
        with (mock.patch.dict(os.environ, {"SHOWRUNNER_STATE_DIR": str(registration.parents[1])}),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(review.sample([path], 0, since=datetime.fromisoformat("2026-10-12T00:00:00+00:00")), 0)
        self.assertIn("replies with unregistered questions: 0", output.getvalue())

    def test_registration_history_outside_the_sample_window_does_not_match(self) -> None:
        question = "Would you retry the build?"
        path = self.transcript("questions.jsonl", [("2026-10-13T10:00:00Z", question, "end_turn")])
        directory = self.root / "showrunner/outstanding"
        directory.mkdir(parents=True)
        _ = (directory / "build.json").write_text(json.dumps([{"text": "Choose a compiler.", "since": "2026-10-01T10:00"}]))
        records = [review.QuestionRegistration(time="2026-10-11T10:00:00+00:00", text=question),
                   review.QuestionRegistration(time="2026-10-12T00:00:00+00:00", text="Approve the release.")]
        _ = (directory / "build.history.jsonl").write_text("\n".join(json.dumps(item) for item in records) + "\n{bad\n")
        cutoff = datetime.fromisoformat("2026-10-12T00:00:00+00:00")
        with (mock.patch.dict(os.environ, {"SHOWRUNNER_STATE_DIR": str(directory.parent)}),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(review.registered_questions(cutoff), ["Choose a compiler.", "Approve the release."])
            self.assertEqual(review.sample([path], 0, since=cutoff), 0)
        self.assertIn("replies with unregistered questions: 1", output.getvalue())

    def test_rewrite_recheck_must_pass_before_the_rewritten_push_is_approved(self) -> None:
        revised = replace(self.draft, body="Choose whether to retry the nightly build.")
        first = verdict(self.draft, "rewrite", body=revised.body)
        for second, expected in ((verdict(revised), review.Rewritten),
                                 (verdict(revised, "rewrite", body="Choose retry."), review.Failed)):
            replies = iter((first, second))
            def runner(argv: list[str], *, timeout: float, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
                value = next(replies) if Path(argv[0]).name == "claude" else first
                return FakeReviewer(value)(argv, timeout=timeout, env=env)
            with self.subTest(expected=expected), mock.patch.object(review, "review_timeout_seconds", return_value=60) as configured:
                result = review.review(self.draft, False, runner=runner)
                self.assertIsInstance(result, expected)
                configured.assert_called_once()

    def test_unavailable_rewrite_check_keeps_original_failures_in_the_delivery_log(self) -> None:
        revised = replace(self.draft, body="Choose whether to retry the nightly build.")
        cases: tuple[tuple[str, Literal["none", "timeout", "json"], VerdictJSON], ...] = (
            ("timeout", "timeout", verdict(revised)),
            ("json", "json", verdict(revised)),
            ("validation", "none", verdict(revised, action=None)),
        )
        for label, failure, second in cases:
            with self.subTest(failure=label):
                first = verdict(self.draft, "rewrite", body=revised.body)
                passes = iter((FakeReviewer(first), FakeReviewer(second, failure=failure)))

                def runner(argv: list[str], *, timeout: float, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
                    selected = next(passes) if Path(argv[0]).name == "claude" else FakeReviewer(first)
                    return selected(argv, timeout=timeout, env=env)

                outcome = review.review(self.draft, False, runner=runner)
                self.assertIsInstance(outcome, review.Unreviewed)
                self.assertEqual(review.best_version(self.draft, outcome), self.draft)
                review.log_review(self.draft, outcome, review.Sent(self.draft))
                entry = self.entries()[-1]
                self.assertIn("reader-starts-cold", cast(list[str], entry["failed_rules"]))
                self.assertEqual(entry["draft"], entry["sent"])
                self.assertTrue(cast(str, entry["verdict"]).startswith("unreviewed:"))

    def test_failed_rewrite_check_logs_only_failures_from_the_draft_that_was_sent(self) -> None:
        revised = replace(self.draft, body="Choose whether to retry the nightly build.")
        first = verdict(self.draft, "rewrite", body=revised.body)
        second = verdict(revised, "rewrite", action=None)
        replies = iter((first, second))

        def runner(argv: list[str], *, timeout: float, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
            value = next(replies) if Path(argv[0]).name == "claude" else first
            return FakeReviewer(value)(argv, timeout=timeout, env=env)

        outcome = review.review(self.draft, False, runner=runner)
        self.assertIsInstance(outcome, review.Unreviewed)
        self.assertEqual(review.best_version(self.draft, outcome), self.draft)
        review.log_review(self.draft, outcome, review.Sent(self.draft))
        entry = self.entries()[-1]
        self.assertEqual(entry["draft"], entry["sent"])
        self.assertEqual(entry["failed_rules"], ["reader-starts-cold"])
        self.assertEqual(entry["failures"], [{"rule": "reader-starts-cold", "quote": self.draft.body[:10]}])

    def test_first_pass_validation_error_keeps_supported_failures_in_the_log(self) -> None:
        value = verdict(self.draft, "rewrite", action=None)
        outcome = review.review(self.draft, False, runner=FakeReviewer(value))
        self.assertIsInstance(outcome, review.Unreviewed)
        assert isinstance(outcome, review.Unreviewed)
        self.assertEqual(outcome.why, "action form changed")
        review.log_review(self.draft, outcome, review.Sent(self.draft))
        entry = self.entries()[-1]
        self.assertEqual(entry["failed_rules"], ["reader-starts-cold"])
        self.assertEqual(entry["draft"], entry["sent"])

    def test_a_review_limit_caps_the_whole_review_and_a_review_out_of_time_is_unreviewed(self) -> None:
        revised = replace(self.draft, body="Choose whether to retry the nightly build.")
        elapsed = [0.0]
        timeouts: list[float] = []

        def reads(replies: list[VerdictJSON], seconds: float, failure: Literal["none", "timeout"] = "none") -> review.ProcessRunner:
            """A reviewer whose each read takes `seconds` and gives the next reply."""
            left = iter(replies)

            def runner(argv: list[str], *, timeout: float, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
                if Path(argv[0]).name != "claude":
                    return FakeReviewer(verdict(self.draft))(argv, timeout=timeout, env=env)
                timeouts.append(timeout)
                elapsed[0] += seconds
                return FakeReviewer(next(left), failure=failure)(argv, timeout=timeout, env=env)
            return runner

        rewrite = verdict(self.draft, "rewrite", body=revised.body)
        cases: tuple[tuple[review.ProcessRunner, type, list[float]], ...] = (
            # The limit is the reviewer's timeout when it is shorter than the configured 60 s.
            (reads([verdict(self.draft)], 5), review.Approved, [30]),
            (reads([verdict(self.draft)], 30, "timeout"), review.Unreviewed, [30]),
            # Time left after the first read is the timeout of the second.
            (reads([rewrite, verdict(revised)], 12), review.Rewritten, [30, 18]),
            # A rewrite whose first read used up the limit gets no second read.
            (reads([rewrite, verdict(revised)], 31), review.Unreviewed, [30]))
        for runner, expected, read_timeouts in cases:
            elapsed[0], timeouts[:] = 0.0, []
            with (self.subTest(expected=expected, read_timeouts=read_timeouts),
                  mock.patch.object(review, "review_timeout_seconds", return_value=60),
                  mock.patch.object(time, "monotonic", side_effect=lambda: elapsed[0])):
                outcome = review.review(self.draft, False, runner=runner, limit=30)
                self.assertIsInstance(outcome, expected)
                if isinstance(outcome, review.Unreviewed):
                    self.assertEqual(outcome.why, "timeout")
                self.assertEqual(timeouts, read_timeouts)

    def test_two_slow_reviews_fit_the_sender_wait_and_keep_a_live_claim_owned(self) -> None:
        held = self.held()
        review.hold(held)
        elapsed = [0.0]
        revised = replace(self.draft, body="Choose whether to retry the nightly build.")
        replies = iter((verdict(self.draft, "rewrite", body=revised.body), verdict(revised)))
        def runner(argv: list[str], *, timeout: float, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
            if Path(argv[0]).name == "claude":
                elapsed[0] += timeout
                self.assertFalse(review.claim(held.source))
                value = next(replies)
            else:
                value = verdict(self.draft)
            return FakeReviewer(value)(argv, timeout=timeout, env=env)
        with (mock.patch.object(time, "time", side_effect=lambda: 1301 + elapsed[0]),
              mock.patch.object(time, "monotonic", side_effect=lambda: elapsed[0])):
            self.assertTrue(review.claim(held.source))
            self.assertIsInstance(review.review(self.draft, True, runner=runner), review.Rewritten)
        self.assertEqual(elapsed[0], 120)
        self.assertLess(elapsed[0] + pushover.TIMEOUT_S, review.user_send_seconds())

    def test_blocked_rewrite_is_sent_when_its_recheck_withholds(self) -> None:
        draft = replace(self.draft, priority="2")
        revised = replace(draft, body="Choose whether to retry the nightly build.")
        first = verdict(draft, "rewrite", body=revised.body)
        replies = iter((first, verdict(revised, "withhold", failed_rules=[
            {"rule": "push-only-what-the-user-must-do", "quote": revised.body}])))
        def runner(argv: list[str], *, timeout: float, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
            value = next(replies) if Path(argv[0]).name == "claude" else first
            return FakeReviewer(value)(argv, timeout=timeout, env=env)
        outcome = REAL_REVIEW(draft, False, runner=runner)
        self.assertIsInstance(outcome, review.Withheld)
        self.assertEqual(review.best_version(draft, outcome), revised)
        keys = self.root / "keys"
        _ = keys.write_text("PUSHOVER_USER=test-user\nPUSHOVER_TOKEN=test-token\n")
        post = mock.Mock(return_value=(200, b'{"status":1}'))
        with mock.patch.object(review, "review", return_value=outcome), redirect_stdout(io.StringIO()):
            self.assertEqual(pushover.send(["--priority", "2", "--source", draft.source,
                "--action", cast(ActionRequired, draft.action).text, draft.title, draft.body], keys_file=keys, post=post), 0)
        form = urllib.parse.parse_qs(cast(bytes, post.call_args.args[1]).decode())
        self.assertEqual(form["message"], [with_footer(first_line(revised.action) + "\n" + revised.body, draft.source)])
        self.assertIn("sent despite failed review", str(self.entries()[0]["detail"]))

    def test_texts_reviews_every_example_and_approves_only_unchanged_send_results(self) -> None:
        entry = push_texts.PushText("test", "script", "decision", "Choose version", push_texts.ActionTemplate("Choose retry."),
                                   "The check passed on {version}.", (push_texts.PushExample("typical", {"version": "1"}), push_texts.PushExample("long", {"version": "2000"})),
                                   purpose="Test.")
        approvals = self.root / "reviewed.json"
        reviewed: list[str] = []
        def runner(argv: list[str], *, timeout: float, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
            body = "The check passed on 2000." if "2000" in argv[-1] else "The check passed on 1."
            draft = review.Draft("script", "1", "Choose version", ActionRequired("Choose retry."), body, False)
            if Path(argv[0]).name == "claude":
                reviewed.append(body)
            value = verdict(draft, "rewrite", body="The check passed.") if "2000" in body else verdict(draft)
            return FakeReviewer(value)(argv, timeout=timeout, env=env)
        with (mock.patch.object(push_texts, "catalog", return_value=(entry,)), mock.patch.object(push_texts, "REVIEWED_FILE", approvals), redirect_stdout(io.StringIO())):
            self.assertEqual(review.texts([], True, runner=runner), 1)
        self.assertEqual(reviewed, ["The check passed on 1.", "The check passed on 2000."])
        saved = cast(dict[str, dict[str, review.TextApproval]], json.loads(approvals.read_text()))
        self.assertEqual(set(saved["test"]), {"typical"})
        self.assertEqual(saved["test"]["typical"]["sha256"], push_texts.fingerprint(entry))
        self.assertFalse(pushover.LOG_FILE.exists())
        self.assertFalse((self.root / "state/held").exists())

    def test_user_spec_approves_original_text_and_keeps_ask_objections_and_existing_approvals(self) -> None:
        entry = push_texts.PushText("test", "shutdown", "decision", "Owner remote-control is off",
            push_texts.ActionTemplate("run /remote-control"), "Reason: Remote control is off.",
            (push_texts.PushExample("typical", {}),), purpose="Test.", session=push_texts.SessionTemplate("Owner"))
        draft = review.Draft("shutdown", "1", entry.title, ActionRequired("run /remote-control"),
                             entry.body, False, SessionLine("Owner"))
        objections = [review.RuleFailureRecord(rule="reader-starts-cold", quote="Remote control is off.")]
        source = "User, 2026-10-14 16:3x EDT, via the showrunner"
        existing: dict[str, dict[str, review.TextApproval]] = {
            "previous": {"typical": {"sha256": "previous-digest", "time": "yesterday", "reviewer": "opus"}},
            "earlier-user-spec": {"typical": {"sha256": "older-digest", "time": "yesterday",
                "reviewer": "user-spec: earlier source", "objections": objections}},
        }
        approvals = self.root / "reviewed.json"
        runner = FakeReviewer(verdict(draft, "ask", questions=["Which terminal?"], failed_rules=objections))
        with (mock.patch.object(push_texts, "catalog", return_value=(entry,)),
              mock.patch.object(push_texts, "REVIEWED_FILE", approvals), redirect_stdout(io.StringIO())):
            for authority in (review.ReviewerApprovalRequired(), review.UserSpecifiedText(source)):
                with self.subTest(authority=type(authority).__name__):
                    _ = approvals.write_text(json.dumps(existing))
                    self.assertEqual(review.texts(["test"], True, runner=runner, authority=authority), 1)
                    saved = review.text_approvals(cast(object, json.loads(approvals.read_text())))
                    self.assertEqual({key: saved[key] for key in existing}, existing)
                    if isinstance(authority, review.UserSpecifiedText):
                        approval = saved["test"]["typical"]
                        self.assertEqual(approval["sha256"], push_texts.fingerprint(entry))
                        self.assertIn("user-spec: " + source, approval["reviewer"])
                        self.assertEqual(approval.get("objections"), objections)
                    else:
                        self.assertNotIn("test", saved)
        self.assertEqual(len([item for item in runner.calls if Path(item.argv[0]).name == "claude"]), 2)
        self.assertEqual([item["verdict"] for item in self.entries()], ["ask", "ask"])
        self.assertFalse(pushover.LOG_FILE.exists())
        self.assertFalse((self.root / "state/held").exists())

    def test_approving_prunes_retired_examples_only_for_selected_texts_and_keeps_current_failed_approvals(self) -> None:
        entry = push_texts.PushText("selected", "shutdown", "decision", self.draft.title,
            push_texts.ActionTemplate(cast(ActionRequired, self.draft.action).text), self.draft.body,
            (push_texts.PushExample("typical", {}), push_texts.PushExample("new", {})), purpose="Test.")
        other = replace(entry, id="other")
        old: review.TextApproval = {"sha256": "old-digest", "time": "yesterday", "reviewer": "opus"}
        existing = {"selected": {"typical": old, "retired": old}, "other": {"retired": old}}
        approvals = self.root / "reviewed.json"
        runner = FakeReviewer(verdict(self.draft, "ask", questions=["Which terminal?"]))
        cases: tuple[tuple[bool, review.TextApprovalAuthority], ...] = (
            (False, review.REVIEWER_APPROVAL_REQUIRED),
            (True, review.REVIEWER_APPROVAL_REQUIRED),
            (True, review.UserSpecifiedText("User words")),
        )
        with (mock.patch.object(push_texts, "catalog", return_value=(entry, other)),
              mock.patch.object(push_texts, "REVIEWED_FILE", approvals), redirect_stdout(io.StringIO())):
            for approve, authority in cases:
                with self.subTest(approve=approve, authority=type(authority).__name__):
                    original = json.dumps(existing)
                    _ = approvals.write_text(original)
                    self.assertEqual(review.texts(["selected"], approve, runner=runner, authority=authority), 1)
                    if not approve:
                        self.assertEqual(approvals.read_text(), original)
                        continue
                    saved = review.text_approvals(cast(object, json.loads(approvals.read_text())))
                    self.assertEqual(saved["other"], existing["other"])
                    if isinstance(authority, review.UserSpecifiedText):
                        self.assertEqual(set(saved["selected"]), {"typical", "new"})
                        for approval in saved["selected"].values():
                            self.assertEqual(approval["sha256"], push_texts.fingerprint(entry))
                    else:
                        self.assertEqual(saved["selected"], {"typical": old})

    def test_texts_cli_requires_approval_for_user_spec_and_passes_source(self) -> None:
        with mock.patch.object(review, "texts", return_value=0) as texts:
            for arguments in (["texts", "--user-spec", "User words"], ["texts", "--approve", "--user-spec", " "]):
                with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                    _ = review.main(arguments)
                self.assertEqual(error.exception.code, 2)
            texts.assert_not_called()
            self.assertEqual(review.main(["texts", "--id", "shutdown.holdout", "--approve",
                                          "--user-spec", "User words"]), 0)
        texts.assert_called_once_with(["shutdown.holdout"], True, authority=review.UserSpecifiedText("User words"))

    def test_list_and_show_cli_print_the_catalog(self) -> None:
        listing = push_texts.listing(push_texts.catalog())
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(review.main(["list"]), 0)
        self.assertEqual(output.getvalue(), listing + "\n")
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(review.main(["show", "session.needs_go"]), 0)
        shown = output.getvalue()
        self.assertIn("Title: {sender}: {session} waits for your go\nSession: {session}\n", shown)
        self.assertIn("Example (typical):\nTitle: natedev: trunk waits for your go\nSession: trunk\n", shown)
        self.assertNotIn("{", shown.split("Example (typical):")[1])
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(review.main(["show", "no.such"]), 1)
        self.assertEqual(output.getvalue(), f"No template named no.such.\n\n{listing}\n")

    def test_retiring_a_rejected_claim_preserves_a_newer_hold_from_the_same_source(self) -> None:
        original = self.held()
        newer = replace(original, created=2000, questions=["Which terminal?"])
        review.hold(original)
        self.assertTrue(review.claim(original.source))
        review.release(original.source)
        review.hold(newer)
        review.retire_claim(original)
        found = review.held_for(original.source)
        self.assertIsInstance(found, review.LiveHold)
        assert isinstance(found, review.LiveHold)
        self.assertEqual(found.hold, newer)
        self.assertIsInstance(review.post_claim(original, lambda: self.fail("retired post")), review.ClaimCancelled)

    def test_withhold_is_only_a_push_verdict_and_has_supported_findings(self) -> None:
        value = verdict(self.draft, "withhold", failed_rules=[{"rule": "push-only-what-the-user-must-do", "quote": self.draft.body}])
        self.assertIsInstance(review.validate(value, self.draft, False), review.Withheld)
        self.assertIsInstance(review.validate(value, self.draft, False, reply=True), review.Unreviewed)

    def test_send_keeps_the_whole_draft(self) -> None:
        result = self.reviewed(verdict(self.draft))
        self.assertIsInstance(result, review.Approved)
        assert isinstance(result, review.Approved)
        self.assertEqual(result.draft, self.draft)

    def test_rewrite_keeps_source_priority_and_action_form(self) -> None:
        result = self.reviewed(verdict(self.draft, "rewrite", title="Build stopped", body="Choose whether to retry.",
                                       failed_rules=[{"rule": "short-plain-sentences", "quote": self.draft.body}]))
        self.assertIsInstance(result, review.Rewritten)
        assert isinstance(result, review.Rewritten)
        self.assertEqual((result.version.source, result.version.priority, result.version.action),
                         (self.draft.source, self.draft.priority, self.draft.action))
        self.assertEqual((result.version.title, result.version.body), ("Build stopped", "Choose whether to retry."))
        self.assertEqual(result.failed_rules, ["short-plain-sentences"])

    def test_rewrite_posts_the_action_line_once(self) -> None:
        keys = self.root / "keys"
        _ = keys.write_text("PUSHOVER_USER=test-user\nPUSHOVER_TOKEN=test-token\n")
        for action in (self.draft.action, NoActionRequired()):
            draft = replace(self.draft, action=action, priority="0")
            header = first_line(action)
            for blank_lines in ("", "\n", "\n\n"):
                with self.subTest(action=action, blank_lines=blank_lines):
                    body = "The build stopped. Choose whether to retry."
                    value = verdict(draft, "rewrite", body=header + "\n" + blank_lines + body)
                    result = review.validate(value, draft, False)
                    self.assertIsInstance(result, review.Rewritten)
                    assert isinstance(result, review.Rewritten)
                    expected_body = ("\n" if blank_lines == "\n\n" else "") + body
                    self.assertEqual(result.version.body, expected_body)
                    post = mock.Mock(return_value=(200, b'{"status":1}'))
                    with redirect_stdout(io.StringIO()):
                        self.assertEqual(pushover.deliver(result.version, keys, post), 0)
                    form = urllib.parse.parse_qs(cast(bytes, post.call_args.args[1]).decode())
                    self.assertEqual(form["message"], [with_footer(header + "\n" + expected_body, draft.source)])
                    self.assertEqual(form["message"][0].splitlines().count(header), 1)

    def test_review_action_prefix_is_removed_before_body_validation(self) -> None:
        header = first_line(self.draft.action)
        value = verdict(self.draft, "rewrite", action=header, body=header + "\n\n" + self.draft.body)
        result = self.reviewed(value)
        self.assertIsInstance(result, review.Rewritten)
        assert isinstance(result, review.Rewritten)
        self.assertEqual(result.version.action, self.draft.action)
        self.assertEqual(result.version.body, self.draft.body)
        self.assertEqual(pushover.posted_message(result.version.action, result.version.body),
                         header + "\n" + self.draft.body)
        for action in ("Action: ", "Action: none"):
            with self.subTest(action=action):
                self.assertIsInstance(self.reviewed(verdict(self.draft, "rewrite", action=action)), review.Unreviewed)

    def test_send_accepts_an_unchanged_draft_after_removing_its_repeated_header(self) -> None:
        for action in (self.draft.action, NoActionRequired()):
            draft = replace(self.draft, action=action, priority="0")
            header = first_line(action)
            for separator in ("\n", "\n\n"):
                with self.subTest(action=action, separator=separator):
                    value = verdict(draft, body=header + separator + draft.body)
                    result = review.validate(value, draft, False)
                    self.assertIsInstance(result, review.Approved)
                    assert isinstance(result, review.Approved)
                    self.assertEqual(result.draft, draft)
        value = verdict(self.draft, action=first_line(self.draft.action),
                        body=first_line(self.draft.action) + "\n" + self.draft.body)
        self.assertIsInstance(self.reviewed(value), review.Approved)
        value = verdict(self.draft, body=first_line(self.draft.action) + "\nDifferent body.")
        self.assertIsInstance(self.reviewed(value), review.Unreviewed)

    def test_body_header_removal_requires_an_exact_first_line(self) -> None:
        header = first_line(self.draft.action)
        for body in (header + " extra\nDetails.", "Details.\n" + header, "\n" + header):
            with self.subTest(body=body):
                result = self.reviewed(verdict(self.draft, "rewrite", body=body))
                self.assertIsInstance(result, review.Rewritten)
                assert isinstance(result, review.Rewritten)
                self.assertEqual(result.version.body, body)
        result = self.reviewed(verdict(self.draft, "rewrite", body=header))
        self.assertIsInstance(result, review.Rewritten)
        assert isinstance(result, review.Rewritten)
        self.assertEqual(result.version.body, "")

    def test_ask_retains_its_questions_so_the_push_is_not_sent(self) -> None:
        value = verdict(self.draft, "ask", questions=["Which build stopped?"], failed_rules=[{"rule": "reader-starts-cold", "quote": self.draft.body}])
        first = self.reviewed(value)
        self.assertIsInstance(first, review.Questions)
        assert isinstance(first, review.Questions)
        self.assertEqual(first.questions, ["Which build stopped?"])
        self.assertIsInstance(self.reviewed(value, revision=True), review.Questions)

    def test_false_length_failures_return_the_original_unchanged(self) -> None:
        for rule in ("push-body-under-500", "push-title-under-45"):
            result = self.reviewed(verdict(self.draft, "ask", title="Different title",
                questions=[], failed_rules=[{"rule": rule, "quote": self.draft.body}]))
            self.assertIsInstance(result, review.Approved)
            assert isinstance(result, review.Approved)
            self.assertEqual(result.draft, self.draft)

    def test_unsupported_quotes_are_dropped_while_supported_failures_stay(self) -> None:
        result = self.reviewed(verdict(self.draft, "rewrite", body="Choose whether to retry.", failed_rules=[
            {"rule": "reader-starts-cold", "quote": "Words absent from this draft"},
            {"rule": "short-plain-sentences", "quote": "The build stopped."},
            {"rule": "push-body-under-500", "quote": self.draft.body}]))
        self.assertIsInstance(result, review.Rewritten)
        assert isinstance(result, review.Rewritten)
        self.assertEqual(result.failed_rules, ["short-plain-sentences"])
        self.assertEqual(result.failures, (review.RuleFailure("short-plain-sentences", "The build stopped."),))
        with redirect_stdout(io.StringIO()) as output:
            review.print_review(self.draft, result)
        self.assertIn('failure: {"rule": "short-plain-sentences", "quote": "The build stopped."}', output.getvalue())
        self.assertIsInstance(self.reviewed(verdict(self.draft, "rewrite", failed_rules=[
            {"rule": "reader-starts-cold", "quote": "Not in the original"}])), review.Approved)

    def test_a_body_that_gives_a_second_action_fails_push_one_action(self) -> None:
        action = ActionRequired("On natedev, in the tile pane, approve the prompt or add an allow rule")
        draft = review.Draft("writer one", "1", "tile waits for you", action,
                             "Auto mode refused 1 command in tile. Type ! kill -TERM -3137969 in the cargo-liner session.", True)
        result = review.validate(verdict(draft), draft, False)
        self.assertIsInstance(result, review.Failed)
        assert isinstance(result, review.Failed)
        self.assertEqual(result.failed_rules, ["push-one-action"])
        self.assertEqual(result.failures, (review.RuleFailure("push-one-action",
            'the body says to type "kill -TERM -3137969", which the Action line does not hold; '
            + "the body names the cargo-liner pane or session, which the Action line does not name"),))
        clean = replace(draft, body="Auto mode refused 1 command in tile. The tile build waits for it.")
        # Code decides the rule: a model report on a clean draft is dropped, and a rewrite that adds a second action fails.
        self.assertIsInstance(review.validate(verdict(clean, failed_rules=[
            {"rule": "push-one-action", "quote": clean.body}]), clean, False), review.Approved)
        rewrite = review.validate(verdict(clean, "rewrite", body=draft.body), clean, False)
        self.assertIsInstance(rewrite, review.Failed)
        assert isinstance(rewrite, review.Failed)
        self.assertIn("push-one-action", rewrite.failed_rules)

    def test_oversized_rewrite_remains_a_review_failure_instead_of_a_reviewer_fault(self) -> None:
        result = self.reviewed(verdict(self.draft, "rewrite", body="B" * 500, failed_rules=[
            {"rule": "reader-starts-cold", "quote": "The build stopped."}]))
        self.assertIsInstance(result, review.Failed)
        assert isinstance(result, review.Failed)
        self.assertIn("reader-starts-cold", result.failed_rules)
        self.assertIn("push-body-under-500", result.failed_rules)
        review.log_review(self.draft, result, dry=True)
        self.assertEqual(self.entries()[0]["verdict"], "failed")

    def test_length_counts_check_original_and_proposed_text_at_the_boundaries(self) -> None:
        for field, boundary, rule in (("title", 45, "push-title-under-45"),
                                     ("body", 500 - len(first_line(self.draft.action) + "\n"),
                                      "push-body-under-500")):
            for size in (boundary - 1, boundary):
                draft = replace(self.draft, **{field: "X" * size})
                result = review.validate(verdict(draft, "rewrite", title="Build stopped", body="Choose retry.", failed_rules=[]), draft, False)
                if size < boundary:
                    self.assertIsInstance(result, review.Approved)
                else:
                    self.assertIsInstance(result, review.Rewritten)
                    assert isinstance(result, review.Rewritten)
                    self.assertEqual(result.failed_rules, [rule])
            proposed = self.reviewed(verdict(self.draft, "rewrite", **{field: "X" * boundary}))
            self.assertIsInstance(proposed, review.Failed)

    def test_message_length_counts_action_and_no_action_headers_and_separator(self) -> None:
        for action in (self.draft.action, NoActionRequired()):
            for length in (499, 500):
                with self.subTest(action=action, length=length):
                    draft = replace(self.draft, action=action, priority="0",
                        body="B" * (length - len(first_line(action) + "\n")))
                    result = review.validate(verdict(draft, "rewrite", body="The build stopped.", failed_rules=[]), draft, False)
                    if length == 499:
                        self.assertIsInstance(result, review.Approved)
                        assert isinstance(result, review.Approved)
                        self.assertEqual(result.draft, draft)
                    else:
                        self.assertIsInstance(result, review.Rewritten)
                        assert isinstance(result, review.Rewritten)
                        self.assertEqual(result.failed_rules, ["push-body-under-500"])
                        self.assertEqual(result.failures[0].quote, first_line(action) + "\n" + draft.body)
                    proposed = review.validate(verdict(replace(self.draft, action=action, priority="0"), "rewrite", body=draft.body), replace(self.draft, action=action, priority="0"), False)
                    if length == 499:
                        self.assertIsInstance(proposed, review.Rewritten)
                    else:
                        self.assertIsInstance(proposed, review.Failed)

    def test_action_cannot_be_removed_emptied_or_added(self) -> None:
        for action in (None, "", "   ", "none"):
            with self.subTest(action=action):
                self.assertIsInstance(self.reviewed(verdict(self.draft, "rewrite", action=action)), review.Unreviewed)
        no_action = replace(self.draft, action=NoActionRequired(), priority="0")
        result = review.review(no_action, False, runner=FakeReviewer(verdict(no_action, "rewrite", action="Restart it.")))
        self.assertIsInstance(result, review.Unreviewed)

    def test_wire_limits_include_the_action_line(self) -> None:
        available = 1024 - len("Action: " + cast(ActionRequired, self.draft.action).text + "\n")
        maximum = verdict(self.draft, "rewrite", title="T" * 250, body="B" * available)
        self.assertIsInstance(self.reviewed(maximum), review.Failed)
        repeated = verdict(self.draft, "rewrite", action=first_line(self.draft.action),
                           body=first_line(self.draft.action) + "\n\n" + "B" * available)
        self.assertIsInstance(self.reviewed(repeated), review.Failed)
        for changes in ({"title": "T" * 251}, {"body": "B" * (available + 1)}):
            self.assertIsInstance(self.reviewed(verdict(self.draft, "rewrite", **changes)), review.Unreviewed)

    def test_invalid_verdict_fields_do_not_become_approved_messages(self) -> None:
        invalid: list[dict[str, object]] = [
            {"verdict": "drop"}, {"title": 3}, {"body": None}, {"failed_rules": [3]},
            {"verdict": "ask", "questions": ["a", "b", "c", "d"]},
            {"verdict": "ask", "questions": [], "failed_rules": [{"rule": "reader-starts-cold", "quote": self.draft.body}]},
            {"verdict": "ask", "questions": [""], "failed_rules": [{"rule": "reader-starts-cold", "quote": self.draft.body}]},
            {"questions": ["Unexpected question"]}, {"title": "Changed while claiming send"},
        ]
        for changes in invalid:
            with self.subTest(changes=changes):
                self.assertIsInstance(self.reviewed(verdict(self.draft, **changes)), review.Unreviewed)

    def test_process_failures_return_unreviewed(self) -> None:
        failures: list[Literal["timeout", "missing", "exit", "json", "quota"]] = [
            "timeout", "missing", "exit", "json", "quota",
        ]
        for failure in failures:
            with self.subTest(failure=failure):
                result = review.review(self.draft, False, runner=FakeReviewer(verdict(self.draft), failure=failure))
                self.assertIsInstance(result, review.Unreviewed)
                assert isinstance(result, review.Unreviewed)
                self.assertTrue(result.why)

    def test_unexpected_review_faults_send_the_original_push(self) -> None:
        keys = self.root / "keys"
        _ = keys.write_text("PUSHOVER_USER=test-user\nPUSHOVER_TOKEN=test-token\n")

        for target, error in (("reviewer_environment", ImportError("account import failed")),
                              ("reviewer_environment", RuntimeError("account lookup failed")),
                              ("runner", RuntimeError("process adapter failed"))):
            with self.subTest(target=target, error=error):
                posted: list[bytes] = []

                def post(_url: str, data: bytes) -> tuple[int, bytes]:
                    posted.append(data)
                    return 200, b'{"status":1}'

                selected = mock.Mock(side_effect=error) if target == "runner" else FakeReviewer(verdict(self.draft))

                def read(draft: review.Draft, revision: bool) -> review.ReviewOutcome:
                    return REAL_REVIEW(draft, revision, runner=selected)

                with mock.patch.object(review, "reviewer_environment", return_value=({}, "claude2"),
                                       side_effect=error if target != "runner" else None):
                    outcome = REAL_REVIEW(self.draft, False, runner=selected)
                    self.assertIsInstance(outcome, review.Unreviewed)
                    assert isinstance(outcome, review.Unreviewed)
                    self.assertEqual(outcome.why, f"review unavailable: {type(error).__name__}: {error}")
                    with (mock.patch.object(review, "review", side_effect=read),
                          redirect_stdout(io.StringIO())):
                        self.assertEqual(pushover.send(
                            ["--source", self.draft.source, "--priority", self.draft.priority,
                             "--action", cast(ActionRequired, self.draft.action).text,
                             self.draft.title, self.draft.body], keys, post), 0)
                self.assertEqual(len(posted), 1)
                form = urllib.parse.parse_qs(posted[0].decode())
                self.assertEqual(form["title"], [self.draft.title])
                self.assertEqual(form["message"], [with_footer(first_line(self.draft.action) + "\n" + self.draft.body,
                                                               self.draft.source)])
                self.assertIn(outcome.why, str(self.entries()[-1]["verdict"]))

    def test_missing_or_unreadable_guide_never_starts_claude(self) -> None:
        for missing in (False, True):
            runner = FakeReviewer(verdict(self.draft), guide_ok=False)
            with mock.patch.object(review, "GUIDE", self.root / "absent" if missing else review.GUIDE):
                result = review.review(self.draft, False, runner=runner)
            self.assertIsInstance(result, review.Unreviewed)
            assert isinstance(result, review.Unreviewed)
            self.assertEqual(result.why, "no guide")
            self.assertFalse(any(Path(call.argv[0]).name == "claude" for call in runner.calls))

    def test_cli_loads_push_rules_once_and_disables_tools_and_settings(self) -> None:
        runner = FakeReviewer(verdict(self.draft))
        with mock.patch.object(shutil, "which", return_value=str(self.root / "bin/claude")) as which:
            _ = review.review(self.draft, False, runner=runner)
        which.assert_called_once_with("claude")
        guide_calls = [call for call in runner.calls if call.argv[0] == "zsh"]
        self.assertEqual(len(guide_calls), 1)
        self.assertEqual(guide_calls[0].argv[-2:], ["--tags", "push,all"])
        command = next(call.argv for call in runner.calls if Path(call.argv[0]).name == "claude")
        self.assertEqual(command[0], str(self.root / "bin/claude"))
        for flag, value in (("--model", "sonnet"), ("--effort", "high"), ("--output-format", "json"),
                            ("--tools", ""), ("--setting-sources", "")):
            self.assertEqual(command[command.index(flag) + 1], value)
        self.assertIn("-p", command)
        self.assertIn("--no-session-persistence", command)
        self.assertIn("Loaded push rules", command[command.index("--system-prompt") + 1])
        self.assertIn("without the `Action:` prefix", command[command.index("--system-prompt") + 1])
        self.assertIn("Body (text after the action line; never repeat it):", command[-1])
        self.assertIn(self.draft.body, command[-1])
        self.assertIn("Revision: false", command[-1])
        schema = cast(dict[str, object], json.loads(command[command.index("--json-schema") + 1]))
        self.assertEqual(set(cast(list[str], schema["required"])),
                         {"verdict", "title", "action", "body", "questions", "failed_rules"})
        self.assertTrue(all(0 < call.timeout <= 60 for call in runner.calls))

    def test_unresolved_registry_uses_opus_medium(self) -> None:
        runner = FakeReviewer(verdict(self.draft), assignment_ok=False)
        _ = review.review(self.draft, False, runner=runner)
        command = next(call.argv for call in runner.calls if Path(call.argv[0]).name == "claude")
        self.assertEqual(command[command.index("--model") + 1], "opus")
        self.assertEqual(command[command.index("--effort") + 1], "medium")

    def test_setup_and_reviewer_share_one_configured_budget_read_once(self) -> None:
        runner = FakeReviewer(verdict(self.draft))
        elapsed = [0.0]

        def process(argv: list[str], *, timeout: float, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
            self.assertLessEqual(timeout, 90 - elapsed[0])
            if argv[0] == "zsh":
                elapsed[0] += 88
            elif argv[0] == "bash":
                elapsed[0] += 1
            else:
                self.assertLessEqual(timeout, 1)
                elapsed[0] += timeout
                raise subprocess.TimeoutExpired(argv, timeout)
            return runner(argv, timeout=timeout, env=env)

        with (mock.patch.object(time, "monotonic", side_effect=lambda: elapsed[0]),
              mock.patch.object(review, "review_timeout_seconds", return_value=90) as config):
            result = review.review(self.draft, False, runner=process)
        config.assert_called_once()
        self.assertIsInstance(result, review.Unreviewed)
        assert isinstance(result, review.Unreviewed)
        self.assertEqual(result.why, "timeout")
        self.assertEqual(elapsed[0], 90)

    def test_claude2_reviewer_uses_its_numbered_folder_instead_of_the_callers_login(self) -> None:
        runner = FakeReviewer(verdict(self.draft))
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "writer token",
                                         "ANTHROPIC_API_KEY": "writer key", "CLAUDE_CONFIG_DIR": "writer config",
                                         "MESSAGE_TEST_KEEP": "kept"}):
            _ = review.review(self.draft, False, runner=runner)
        for call in runner.calls:
            for key in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"):
                self.assertNotIn(key, call.environment)
            self.assertEqual(call.environment["CLAUDE_CONFIG_DIR"], str(self.root / ".claude2"))
            self.assertEqual(call.environment["MESSAGE_TEST_KEEP"], "kept")

    def test_named_unit_account_replaces_the_callers_directory(self) -> None:
        runner = FakeReviewer(verdict(self.draft))
        _ = (self.root / "notes/claude3.md").write_text("---\nlogin: unit@example.invalid\n---\n")
        self.assertEqual(accounts.setup("claude3"), [])
        directory = accounts.account_dir("claude3")
        _ = (directory / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "unit@example.invalid"}}))
        with (mock.patch.object(accounts, "role_account", return_value=accounts.NamedAccount("claude3", directory)) as select,
              mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "writer config", "ANTHROPIC_API_KEY": "writer key"})):
            result = review.review(self.draft, False, runner=runner)
        select.assert_called_once_with("unit")
        self.assertEqual(result.metadata.account, "claude3")
        for call in runner.calls:
            self.assertEqual(call.environment["CLAUDE_CONFIG_DIR"], str(directory))
            self.assertNotIn("ANTHROPIC_API_KEY", call.environment)

    def test_log_retains_draft_sent_version_and_review_metadata(self) -> None:
        result = self.reviewed(verdict(self.draft, "rewrite", body="Choose whether to retry.",
                                       failed_rules=[{"rule": "short-plain-sentences", "quote": self.draft.body}]))
        assert isinstance(result, review.Rewritten)
        review.log_review(self.draft, result, review.Sent(result.version))
        entry = self.entries()[0]
        self.assertEqual(entry["source"], self.draft.source)
        self.assertEqual(entry["priority"], "1")
        self.assertEqual(entry["verdict"], "rewrite")
        self.assertEqual(entry["failed_rules"], ["short-plain-sentences"])
        self.assertEqual(cast(dict[str, object], entry["draft"])["body"], self.draft.body)
        self.assertEqual(cast(dict[str, object], entry["sent"])["body"], "Choose whether to retry.")
        self.assertIsNotNone(datetime.fromisoformat(str(entry["time"])).utcoffset())
        self.assertGreaterEqual(cast(float, entry["seconds"]), 0)
        self.assertEqual((entry["account"], entry["model"]), ("claude2", "sonnet"))

    def held(self, created: float = 1000) -> review.Held:
        return review.Held(self.draft.source, created, self.draft,
                           replace(self.draft, body="Choose whether to retry."), ["Which build stopped?"])

    def hold_file(self) -> Path:
        digest = hashlib.sha256(self.draft.source.encode()).hexdigest()[:16]
        return self.root / "state/held" / (digest + ".json")

    def test_hold_round_trip_uses_source_hash_and_schedules_due(self) -> None:
        held = self.held()
        self.assertIsInstance(review.held_for(held.source), review.NoHold)
        with mock.patch.object(review, "schedule") as schedule:
            review.hold(held)
        schedule.assert_called_once()
        self.assertTrue(self.hold_file().is_file())
        record = cast(dict[str, object], json.loads(self.hold_file().read_text()))
        self.assertEqual((record["source"], record["priority"], record["created"]), (held.source, "1", 1000))
        found = review.held_for(held.source)
        self.assertIsInstance(found, review.LiveHold)
        assert isinstance(found, review.LiveHold)
        self.assertEqual(found.hold, held)
        review.release(held.source)
        self.assertIsInstance(review.held_for(held.source), review.NoHold)

    def test_unreadable_hold_is_quarantined_and_logged(self) -> None:
        path = self.hold_file()
        path.parent.mkdir(parents=True)
        _ = path.write_text("not JSON")
        found = review.held_for(self.draft.source)
        self.assertIsInstance(found, review.UnreadableHold)
        self.assertFalse(path.exists())
        self.assertEqual(len(list((path.parent / "bad").iterdir())), 1)
        self.assertIsInstance(review.held_for(self.draft.source), review.NoHold)
        self.assertIn("unreadable hold", str(self.entries()[0]["verdict"]))

    def test_notifier_failure_leaves_no_hold(self) -> None:
        with mock.patch.object(review, "schedule", side_effect=OSError("notifier unavailable")):
            with self.assertRaises(OSError):
                review.hold(self.held())
        self.assertFalse(self.hold_file().exists())

    def test_claim_has_one_owner_and_restore_allows_retry(self) -> None:
        review.hold(self.held())
        self.assertTrue(review.claim(self.draft.source))
        self.assertFalse(review.claim(self.draft.source))
        self.assertFalse(self.hold_file().exists())
        self.assertTrue(self.hold_file().with_suffix(".sending").exists())
        review.restore(self.draft.source)
        self.assertTrue(self.hold_file().exists())
        self.assertTrue(review.claim(self.draft.source))
        review.release(self.draft.source)
        self.assertIsInstance(review.held_for(self.draft.source), review.NoHold)

    def test_due_posts_once_only_after_five_minutes(self) -> None:
        review.hold(self.held())
        keys = self.root / "keys"
        _ = keys.write_text("PUSHOVER_USER=test-user\nPUSHOVER_TOKEN=test-token\n")
        calls: list[bytes] = []

        def post(_url: str, data: bytes) -> tuple[int, bytes]:
            calls.append(data)
            self.assertTrue(self.hold_file().with_suffix(".sending").exists())
            # A second due call cannot claim the first call's delivery.
            self.assertEqual(review.due(post=post, keys_file=keys), 0)
            return 200, b'{"status":1}'

        with mock.patch.object(time, "time", return_value=1299):
            self.assertEqual(review.due(post=post, keys_file=keys), 0)
        self.assertEqual(calls, [])
        with mock.patch.object(time, "time", return_value=1301):
            self.assertEqual(review.due(post=post, keys_file=keys), 0)
            self.assertEqual(review.due(post=post, keys_file=keys), 0)
        self.assertEqual(len(calls), 1)
        self.assertIn(b"priority=1", calls[0])
        self.assertIsInstance(review.held_for(self.draft.source), review.NoHold)
        self.assertIn("sent after hold expired", pushover.LOG_FILE.read_text())

    def test_failed_expiry_restores_hold_then_retries(self) -> None:
        review.hold(self.held())
        keys = self.root / "keys"
        _ = keys.write_text("PUSHOVER_USER=test-user\nPUSHOVER_TOKEN=test-token\n")

        def refused(_url: str, _data: bytes) -> tuple[int, bytes]:
            return 500, b'{"status":0}'

        def sent(_url: str, _data: bytes) -> tuple[int, bytes]:
            return 200, b'{"status":1}'

        with mock.patch.object(time, "time", return_value=1301):
            self.assertEqual(review.due(post=refused, keys_file=keys), 1)
            self.assertTrue(self.hold_file().exists())
            self.assertFalse(self.hold_file().with_suffix(".sending").exists())
            self.assertIn("expired send failed", str(self.entries()[0]["verdict"]))
            self.assertEqual(review.due(post=sent, keys_file=keys), 0)
        self.assertIsInstance(review.held_for(self.draft.source), review.NoHold)

    def test_due_recovers_a_stopped_claim_only_after_its_budget(self) -> None:
        review.hold(self.held())
        os.utime(self.hold_file(), (1000, 1000))
        with mock.patch.object(time, "time", return_value=1301):
            self.assertTrue(review.claim(self.draft.source))
        self.assertEqual(self.hold_file().with_suffix(".sending").stat().st_mtime, 1301)
        keys = self.root / "keys"
        _ = keys.write_text("PUSHOVER_USER=test-user\nPUSHOVER_TOKEN=test-token\n")
        post = mock.Mock(return_value=(200, b'{"status":1}'))
        with mock.patch.object(time, "time", return_value=1301 + review.claim_stale_seconds() - 1):
            self.assertEqual(review.due(post=post, keys_file=keys), 0)
        post.assert_not_called()
        with (mock.patch.object(time, "time", return_value=1301 + review.claim_stale_seconds()),
              redirect_stdout(io.StringIO())):
            self.assertEqual(review.due(post=post, keys_file=keys), 0)
        post.assert_called_once()
        self.assertIsInstance(review.held_for(self.draft.source), review.NoHold)

    def test_revision_push_takes_over_a_stopped_claim(self) -> None:
        review.hold(self.held())
        with mock.patch.object(time, "time", return_value=1301):
            self.assertTrue(review.claim(self.draft.source))
        keys = self.root / "keys"
        _ = keys.write_text("PUSHOVER_USER=test-user\nPUSHOVER_TOKEN=test-token\n")
        posted: list[bytes] = []

        def post(_url: str, data: bytes) -> tuple[int, bytes]:
            posted.append(data)
            self.assertEqual(self.hold_file().with_suffix(".sending").stat().st_mtime,
                             1301 + review.claim_stale_seconds())
            self.assertFalse(review.claim(self.draft.source))
            return 200, b'{"status":1}'

        def read(draft: review.Draft, revision: bool) -> review.ReviewOutcome:
            self.assertTrue(revision)
            return REAL_REVIEW(draft, revision, runner=FakeReviewer(verdict(draft)))

        with (mock.patch.object(time, "time", return_value=1301 + review.claim_stale_seconds()),
              mock.patch.object(review, "review", side_effect=read), redirect_stdout(io.StringIO())):
            self.assertEqual(pushover.send(
                ["--source", self.draft.source, "--priority", self.draft.priority, "--answerable",
                 "--action", cast(ActionRequired, self.draft.action).text,
                 self.draft.title, self.draft.body], keys, post), 0)
        self.assertEqual(len(posted), 1)
        self.assertIsInstance(review.held_for(self.draft.source), review.NoHold)

    def test_delivery_exception_restores_one_hold_and_sends_the_next(self) -> None:
        first = self.held()
        next_draft = replace(self.draft, source="writer two")
        second = review.Held(next_draft.source, 1000, next_draft, next_draft, ["Which build stopped?"])
        holds = sorted((first, second), key=lambda held: review.held_path(held.source))
        for held in holds:
            review.hold(held)
        calls: list[str] = []

        def deliver(draft: review.Draft, _keys: Path, _post: object, *, detail: str) -> int:
            self.assertEqual(detail, "sent after hold expired")
            calls.append(draft.source)
            if len(calls) == 1:
                self.assertTrue(review.held_path(holds[1].source).exists())
                raise RuntimeError("post adapter failed")
            self.assertTrue(review.held_path(holds[0].source).exists())
            return 0

        with (mock.patch.object(time, "time", return_value=1301),
              mock.patch.object(pushover, "deliver", side_effect=deliver)):
            self.assertEqual(review.due(keys_file=self.root / "unused", post=mock.Mock()), 1)
        self.assertEqual(calls, [held.source for held in holds])
        self.assertTrue(review.held_path(holds[0].source).exists())
        self.assertIsInstance(review.held_for(holds[1].source), review.NoHold)
        self.assertIn("expired send failed: RuntimeError: post adapter failed", str(self.entries()[0]["verdict"]))

    def test_interrupted_expiry_leaves_later_holds_unclaimed(self) -> None:
        next_draft = replace(self.draft, source="writer two")
        for held in (self.held(), review.Held(next_draft.source, 1000, next_draft,
                                           next_draft, ["Which build stopped?"])):
            review.hold(held)
        with (mock.patch.object(time, "time", return_value=1301),
              mock.patch.object(pushover, "deliver", side_effect=KeyboardInterrupt)):
            with self.assertRaises(KeyboardInterrupt):
                _ = review.due(keys_file=self.root / "unused", post=mock.Mock())
        directory = self.root / "state/held"
        self.assertEqual(len(list(directory.glob("*.sending"))), 1)
        self.assertEqual(len(list(directory.glob("*.json"))), 1)

    def test_release_reports_delivered_released_posting_and_none(self) -> None:
        held = self.held()
        identity = review.hold_identity(held)
        self.assertEqual(review.release_if_matches(held.source, identity), review.ReleaseOutcome.NONE)
        review.hold(held)
        self.assertEqual(review.release_if_matches(held.source, identity), review.ReleaseOutcome.RELEASED)
        self.assertEqual(review.held_path(held.source).with_suffix(".cancel").read_text(), identity)
        review.hold(held)
        # A cancellation for this exact record stops even a later attempt to claim it.
        self.assertTrue(review.claim(held.source))
        self.assertIsInstance(review.post_claim(held, lambda: self.fail("cancelled post")), review.ClaimCancelled)
        review.held_path(held.source).with_suffix(".cancel").unlink()
        review.hold(held)
        with mock.patch.object(time, "time", return_value=1301):
            self.assertTrue(review.claim(held.source))
            self.assertEqual(review.release_if_matches(held.source, identity), review.ReleaseOutcome.POSTING)
            self.assertIsInstance(review.post_claim(held, lambda: 0), review.ClaimSent)
            self.assertEqual(review.release_if_matches(held.source, identity), review.ReleaseOutcome.DELIVERED)
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(review.main(["release", "--identity", identity, "--", held.source]), 0)
            self.assertEqual(output.getvalue(), "delivered\n")

    def test_release_stops_at_its_deadline_when_the_thread_mutex_is_busy(self) -> None:
        held = self.held()
        elapsed = [0.0]
        def sleep(seconds: float) -> None:
            elapsed[0] += seconds
        acquire = mock.Mock(return_value=False)
        release = mock.Mock()
        mutex = mock.Mock(acquire=acquire, release=release)
        with mock.patch.object(review, "_STATE_MUTEX", mutex), self.assertRaises(TimeoutError):
            _ = review.release_if_matches(held.source, review.hold_identity(held), timeout=0.2,
                                          clock=lambda: elapsed[0], sleep=sleep)
        self.assertAlmostEqual(elapsed[0], 0.2)
        release.assert_not_called()
        acquire.assert_called_with(blocking=False)

    def test_stale_claim_is_cancelled_and_a_resuming_worker_does_not_post(self) -> None:
        held = self.held()
        review.hold(held)
        real_post_claim = review.post_claim
        posted: list[str] = []

        def resume_after_fallback(held: review.Held, send: object) -> review.ClaimDelivery:
            del send
            with mock.patch.object(time, "time", return_value=1301 + review.claim_stale_seconds()):
                self.assertEqual(review.release_if_matches(held.source, review.hold_identity(held)),
                                 review.ReleaseOutcome.RELEASED)
            return real_post_claim(held, lambda: posted.append(held.source) or 0)

        with (mock.patch.object(time, "time", return_value=1301),
              mock.patch.object(review, "post_claim", side_effect=resume_after_fallback)):
            self.assertEqual(review.due(post=mock.Mock(), keys_file=self.root / "unused"), 0)
        self.assertEqual(posted, [])
        self.assertIsInstance(review.held_for(held.source), review.NoHold)

    def test_release_preserves_a_newer_hold_and_claim_with_another_identity(self) -> None:
        original = self.held()
        replacement = replace(original, created=2000, questions=["Which terminal?"])
        review.hold(replacement)
        for claimed in (False, True):
            if claimed:
                self.assertTrue(review.claim(replacement.source))
            self.assertEqual(review.release_if_matches(original.source, review.hold_identity(original)),
                             review.ReleaseOutcome.NONE)
            found = review.held_for(replacement.source)
            assert isinstance(found, review.LiveHold)
            self.assertEqual(found.hold, replacement)

    def test_recovery_drops_a_claim_with_a_completed_delivery_record(self) -> None:
        held = self.held()
        review.hold(held)
        with mock.patch.object(time, "time", return_value=1301):
            self.assertTrue(review.claim(held.source))
            delivered = review.delivered_path(review.hold_identity(held))
            delivered.parent.mkdir(parents=True)
            delivered.touch()
            os.utime(delivered, (1301, 1301))
        with (mock.patch.object(time, "time", return_value=1301 + review.claim_stale_seconds()),
              mock.patch.object(pushover, "deliver", side_effect=AssertionError("duplicate post"))):
            self.assertEqual(review.due(post=mock.Mock(), keys_file=self.root / "unused"), 0)
        self.assertIsInstance(review.held_for(held.source), review.NoHold)

    def test_delivered_records_expire_after_one_hour(self) -> None:
        held = self.held()
        review.hold(held)
        with mock.patch.object(time, "time", return_value=1301):
            self.assertTrue(review.claim(held.source))
            self.assertIsInstance(review.post_claim(held, lambda: 0), review.ClaimSent)
        path = review.delivered_path(review.hold_identity(held))
        with mock.patch.object(time, "time", return_value=1301 + review.DELIVERED_KEEP_SECONDS - 1):
            self.assertEqual(review.due(post=mock.Mock(), keys_file=self.root / "unused"), 0)
            self.assertTrue(path.exists())
        with mock.patch.object(time, "time", return_value=1301 + review.DELIVERED_KEEP_SECONDS):
            self.assertEqual(review.due(post=mock.Mock(), keys_file=self.root / "unused"), 0)
            self.assertFalse(path.exists())

    def test_switch_status_does_not_change_the_switch(self) -> None:
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(review.switch("off"), 0)
            self.assertEqual(review.switch("status"), 0)
            self.assertTrue((self.root / "state/review_off").exists())
            self.assertEqual(review.switch("on"), 0)
            self.assertEqual(review.switch("status"), 0)
        self.assertEqual(output.getvalue().splitlines(), ["review: off", "review: off", "review: on", "review: on"])

    def test_replay_reads_sent_entries_newest_first_without_posting_or_holding(self) -> None:
        for title, outcome in (("old", "sent"), ("failed", "refused"), ("new", "sent, receipt test")):
            pushover.log("script", "0", title, "No action needed.\nbody", outcome)
        original = pushover.LOG_FILE.read_bytes()
        drafts: list[review.Draft] = []

        def approve(draft: review.Draft, revision: bool) -> review.ReviewOutcome:
            self.assertFalse(revision)
            self.assertFalse(draft.answerable)
            drafts.append(draft)
            return review.Approved(draft)

        with (mock.patch.object(review, "review", side_effect=approve),
              mock.patch.object(review, "hold", side_effect=AssertionError("replay hold")),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(review.replay(1), 0)
        self.assertEqual([draft.title for draft in drafts], ["new"])
        self.assertIn("new", output.getvalue())
        self.assertEqual(pushover.LOG_FILE.read_bytes(), original)
        self.assertTrue(self.entries()[0]["replay"])
        self.assertIsNone(self.entries()[0]["sent"])
        drafts.clear()
        with mock.patch.object(review, "review", side_effect=approve), redirect_stdout(io.StringIO()):
            self.assertEqual(review.replay(), 0)
        self.assertEqual([draft.title for draft in drafts], ["new", "old"])

    def test_check_is_dry_even_when_reviewer_asks(self) -> None:
        def ask(draft: review.Draft, _revision: bool) -> review.ReviewOutcome:
            self.assertFalse(draft.answerable)
            return review.Questions(draft, ["Which build stopped?"], ["reader-starts-cold"])

        with (mock.patch.object(review, "review", side_effect=ask),
              mock.patch.object(review, "hold", side_effect=AssertionError("dry hold")),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(review.main(["check", "--title", "Build stopped", "--no-action", "--text", "body"]), 0)
        self.assertIn("ask", output.getvalue())
        self.assertTrue(self.entries()[0]["dry"])
        self.assertIsNone(self.entries()[0]["sent"])
        self.assertFalse(pushover.LOG_FILE.exists())


if __name__ == "__main__":
    _ = unittest.main()
