"""Reviewer and held-push contracts, with local state and fake processes/posts."""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import tempfile
import time
import unittest
import urllib.parse
from contextlib import redirect_stdout
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict, cast, final, override
from unittest import mock

import pushover
import review
from user_action import ActionRequired, NoActionRequired, first_line

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
    failed_rules: list[str]


class ReviewerEnvelope(TypedDict):
    structured_output: VerdictJSON
    is_error: bool


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
        if argv[0] != "claude":
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


def verdict(draft: review.Draft, decision: str = "send", /, **changes: object) -> VerdictJSON:
    value: VerdictJSON = {
        "verdict": decision, "title": draft.title,
        "action": draft.action.text if isinstance(draft.action, ActionRequired) else None,
        "body": draft.body, "questions": [], "failed_rules": [],
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
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"NOTIFY_STATE_DIR": str(self.root / "state")})))
        guide = self.root / "guide"
        guide.mkdir()
        _ = self.enterContext(mock.patch.object(review, "GUIDE", guide))
        _ = self.enterContext(mock.patch.object(accounts, "role_account", return_value=accounts.DefaultAccount()))
        # Calls without an explicit fake must never start a reviewer or notifier.
        _ = self.enterContext(mock.patch.object(subprocess, "run", side_effect=AssertionError("real process")))
        _ = self.enterContext(mock.patch.object(review, "schedule"))
        _ = self.enterContext(mock.patch.object(pushover, "http_post", side_effect=AssertionError("real post")))
        _ = self.enterContext(mock.patch.object(pushover, "LOG_FILE", self.root / "pushes.jsonl"))

    def reviewed(self, value: VerdictJSON, *, revision: bool = False) -> review.ReviewOutcome:
        return review.review(self.draft, revision, runner=FakeReviewer(value))

    def entries(self) -> list[dict[str, object]]:
        path = self.root / "state/review.jsonl"
        return [cast(dict[str, object], json.loads(line)) for line in path.read_text().splitlines()]

    def test_send_keeps_the_whole_draft(self) -> None:
        result = self.reviewed(verdict(self.draft))
        self.assertIsInstance(result, review.Approved)
        assert isinstance(result, review.Approved)
        self.assertEqual(result.draft, self.draft)

    def test_rewrite_keeps_source_priority_and_action_form(self) -> None:
        result = self.reviewed(verdict(self.draft, "rewrite", title="Build stopped", body="Choose whether to retry.",
                                       failed_rules=["short-plain-sentences"]))
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
                    result = REAL_REVIEW(draft, False, runner=FakeReviewer(value))
                    self.assertIsInstance(result, review.Rewritten)
                    assert isinstance(result, review.Rewritten)
                    expected_body = ("\n" if blank_lines == "\n\n" else "") + body
                    self.assertEqual(result.version.body, expected_body)
                    post = mock.Mock(return_value=(200, b'{"status":1}'))
                    with redirect_stdout(io.StringIO()):
                        self.assertEqual(pushover.deliver(result.version, keys, post), 0)
                    form = urllib.parse.parse_qs(cast(bytes, post.call_args.args[1]).decode())
                    self.assertEqual(form["message"], [header + "\n" + expected_body])
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
                    result = REAL_REVIEW(draft, False, runner=FakeReviewer(value))
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

    def test_ask_has_questions_but_a_revision_cannot_ask_again(self) -> None:
        value = verdict(self.draft, "ask", questions=["Which build stopped?"], failed_rules=["reader-starts-cold"])
        first = self.reviewed(value)
        self.assertIsInstance(first, review.Questions)
        assert isinstance(first, review.Questions)
        self.assertEqual(first.questions, ["Which build stopped?"])
        self.assertIsInstance(self.reviewed(value, revision=True), review.Rewritten)

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
        self.assertIsInstance(self.reviewed(maximum), review.Rewritten)
        repeated = verdict(self.draft, "rewrite", action=first_line(self.draft.action),
                           body=first_line(self.draft.action) + "\n\n" + "B" * available)
        self.assertIsInstance(self.reviewed(repeated), review.Rewritten)
        for changes in ({"title": "T" * 251}, {"body": "B" * (available + 1)}):
            self.assertIsInstance(self.reviewed(verdict(self.draft, "rewrite", **changes)), review.Unreviewed)

    def test_invalid_verdict_fields_do_not_become_approved_messages(self) -> None:
        invalid: list[dict[str, object]] = [
            {"verdict": "drop"}, {"title": 3}, {"body": None}, {"failed_rules": [3]},
            {"verdict": "ask", "questions": ["a", "b", "c", "d"]},
            {"verdict": "ask", "questions": []}, {"verdict": "ask", "questions": [""]},
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

                with mock.patch.object(review, "reviewer_environment", return_value=({}, "default"),
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
                self.assertEqual(form["message"], [first_line(self.draft.action) + "\n" + self.draft.body])
                self.assertIn(outcome.why, str(self.entries()[-1]["verdict"]))

    def test_missing_or_unreadable_guide_never_starts_claude(self) -> None:
        for missing in (False, True):
            runner = FakeReviewer(verdict(self.draft), guide_ok=False)
            with mock.patch.object(review, "GUIDE", self.root / "absent" if missing else review.GUIDE):
                result = review.review(self.draft, False, runner=runner)
            self.assertIsInstance(result, review.Unreviewed)
            assert isinstance(result, review.Unreviewed)
            self.assertEqual(result.why, "no guide")
            self.assertFalse(any(call.argv[0] == "claude" for call in runner.calls))

    def test_cli_loads_push_rules_once_and_disables_tools_and_settings(self) -> None:
        runner = FakeReviewer(verdict(self.draft))
        _ = review.review(self.draft, False, runner=runner)
        guide_calls = [call for call in runner.calls if call.argv[0] == "zsh"]
        self.assertEqual(len(guide_calls), 1)
        self.assertEqual(guide_calls[0].argv[-2:], ["--tags", "push,all"])
        command = next(call.argv for call in runner.calls if call.argv[0] == "claude")
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
        self.assertTrue(all(0 < call.timeout <= 20 for call in runner.calls))

    def test_unresolved_registry_uses_opus_medium(self) -> None:
        runner = FakeReviewer(verdict(self.draft), assignment_ok=False)
        _ = review.review(self.draft, False, runner=runner)
        command = next(call.argv for call in runner.calls if call.argv[0] == "claude")
        self.assertEqual(command[command.index("--model") + 1], "opus")
        self.assertEqual(command[command.index("--effort") + 1], "medium")

    def test_setup_and_reviewer_share_one_twenty_second_budget(self) -> None:
        runner = FakeReviewer(verdict(self.draft))
        elapsed = [0.0]

        def process(argv: list[str], *, timeout: float, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
            self.assertLessEqual(timeout, 20 - elapsed[0])
            if argv[0] == "zsh":
                elapsed[0] += 18
            elif argv[0] == "bash":
                elapsed[0] += 1
            else:
                self.assertLessEqual(timeout, 1)
                elapsed[0] += timeout
                raise subprocess.TimeoutExpired(argv, timeout)
            return runner(argv, timeout=timeout, env=env)

        with mock.patch.object(time, "monotonic", side_effect=lambda: elapsed[0]):
            result = review.review(self.draft, False, runner=process)
        self.assertIsInstance(result, review.Unreviewed)
        assert isinstance(result, review.Unreviewed)
        self.assertEqual(result.why, "timeout")
        self.assertEqual(elapsed[0], 20)

    def test_default_account_cannot_use_the_callers_login(self) -> None:
        runner = FakeReviewer(verdict(self.draft))
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "writer token",
                                         "ANTHROPIC_API_KEY": "writer key", "CLAUDE_CONFIG_DIR": "writer config",
                                         "MESSAGE_TEST_KEEP": "kept"}):
            _ = review.review(self.draft, False, runner=runner)
        for call in runner.calls:
            for key in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CONFIG_DIR"):
                self.assertNotIn(key, call.environment)
            self.assertEqual(call.environment["MESSAGE_TEST_KEEP"], "kept")

    def test_named_unit_account_replaces_the_callers_directory(self) -> None:
        runner = FakeReviewer(verdict(self.draft))
        directory = self.root / "unit account"
        with (mock.patch.object(accounts, "role_account", return_value=accounts.NamedAccount("claude 2", directory)) as select,
              mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "writer config", "ANTHROPIC_API_KEY": "writer key"})):
            result = review.review(self.draft, False, runner=runner)
        select.assert_called_once_with("unit")
        self.assertEqual(result.metadata.account, "claude 2")
        for call in runner.calls:
            self.assertEqual(call.environment["CLAUDE_CONFIG_DIR"], str(directory))
            self.assertNotIn("ANTHROPIC_API_KEY", call.environment)

    def test_log_retains_draft_sent_version_and_review_metadata(self) -> None:
        result = self.reviewed(verdict(self.draft, "rewrite", body="Choose whether to retry.",
                                       failed_rules=["short-plain-sentences"]))
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
        self.assertEqual((entry["account"], entry["model"]), ("default", "sonnet"))

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
        with mock.patch.object(time, "time", return_value=1301 + review.CLAIM_STALE_SECONDS - 1):
            self.assertEqual(review.due(post=post, keys_file=keys), 0)
        post.assert_not_called()
        with (mock.patch.object(time, "time", return_value=1301 + review.CLAIM_STALE_SECONDS),
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
                             1301 + review.CLAIM_STALE_SECONDS)
            self.assertFalse(review.claim(self.draft.source))
            return 200, b'{"status":1}'

        def read(draft: review.Draft, revision: bool) -> review.ReviewOutcome:
            self.assertTrue(revision)
            return REAL_REVIEW(draft, revision, runner=FakeReviewer(verdict(draft)))

        with (mock.patch.object(time, "time", return_value=1301 + review.CLAIM_STALE_SECONDS),
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
