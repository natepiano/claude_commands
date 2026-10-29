#!/usr/bin/env python3
"""Tests for pushover.py: keys, request fields, and outcomes, with no network."""

from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast, override
from unittest import mock

import pushover
import review
import push_texts
import silence
from test_review import FakeReviewer, verdict, with_footer
from user_action import (FIX, ActionAllowed, ActionRefused, ActionRequired, ActionUnstated,
                         NoActionRequired, SessionLine, NO_SESSION_LINE, split_session, first_line, parse_user_action, refused_at)

REAL_REVIEW = review.review
PARENT = "python test_pushover.py"


def footed(message: str, sender: str = PARENT, template: str = "none (free text)") -> str:
    return with_footer(message, sender, template)


def approve(draft: review.Draft, revision: bool) -> review.ReviewOutcome:
    del revision
    return review.Approved(draft)


class UserActionTests(unittest.TestCase):
    def test_parse_each_action_state(self) -> None:
        self.assertEqual(parse_user_action(" Run the repair ", False), ActionRequired("Run the repair"))
        self.assertEqual(parse_user_action(None, True), NoActionRequired())
        self.assertEqual(parse_user_action(None, False), ActionUnstated())
        self.assertIsInstance(parse_user_action("run it", True), ActionRefused)

    def test_empty_and_placeholder_actions_are_refused(self) -> None:
        for text in ("", "   ", "none", "NONE", "n/a", "Nothing", "no action", "-"):
            with self.subTest(text=text):
                self.assertIsInstance(parse_user_action(text, False), ActionRefused)

    def test_action_policy_returns_named_allowed_and_refused_states(self) -> None:
        none = NoActionRequired()
        required = ActionRequired("reset Codex")
        self.assertEqual(refused_at(none, 0), ActionAllowed())
        self.assertIsInstance(refused_at(none, 1), ActionRefused)
        self.assertIsInstance(refused_at(none, 2), ActionRefused)
        for priority in (0, 1, 2):
            self.assertEqual(refused_at(required, priority), ActionAllowed())

    def test_session_header_is_parsed_and_kept_above_the_action(self) -> None:
        self.assertEqual(split_session("Session: Owner\nReason: stopped"), (SessionLine("Owner"), "Reason: stopped"))
        self.assertEqual(split_session("Reason: stopped"), (NO_SESSION_LINE, "Reason: stopped"))
        self.assertEqual(first_line(ActionRequired("run it"), SessionLine("Owner")), "Session: Owner\nAction: run it")

    def test_messages_without_session_share_the_explicit_absence_state(self) -> None:
        entry = push_texts.PushText("test", "script", "note", "Done", push_texts.NoActionNeeded(),
                                   "The check passed.", (push_texts.PushExample("typical", {}),), purpose="Test.")
        rendered = push_texts.render_entry(entry, {})
        draft = review.Draft("script", "0", rendered.title, rendered.action, rendered.body, False)
        parsed = pushover.parse(["--no-action", rendered.title, rendered.body])
        assert parsed is not None
        self.assertIs(entry.session, NO_SESSION_LINE)
        self.assertIs(rendered.session, NO_SESSION_LINE)
        self.assertIs(push_texts.RenderedPush("test", "script", "note", rendered.title,
                                            rendered.action, rendered.body, {}).session, NO_SESSION_LINE)
        self.assertIs(draft.session, NO_SESSION_LINE)
        self.assertIs(review._draft(dict(review.draft_record(draft))).session, NO_SESSION_LINE)  # pyright: ignore[reportPrivateUsage]
        self.assertIs(parsed["session"], NO_SESSION_LINE)
        self.assertIs(split_session(rendered.body)[0], NO_SESSION_LINE)

    def test_first_line_states_the_action(self) -> None:
        self.assertEqual(first_line(ActionRequired("choose a branch")), "Action: choose a branch")
        self.assertEqual(first_line(NoActionRequired()), "No action needed.")


class ParentSourceTests(unittest.TestCase):
    def test_parent_source_reads_and_limits_the_parent_command(self) -> None:
        command = "python3 a-very-long-parent-command " + "x" * 100
        completed = subprocess.CompletedProcess(["ps"], 0, stdout=command + "\n", stderr="")
        with mock.patch("pushover.subprocess.run", return_value=completed) as run:
            self.assertEqual(pushover.parent_source(), command[:80])
        self.assertEqual(run.call_args.args[0][:3], ["ps", "-o", "args="])


class PushoverTests(unittest.TestCase):
    keys: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.keys = folder / "env"
        _ = self.keys.write_text("# comment\nPUSHOVER_USER=u123\nPUSHOVER_TOKEN='a456'\n")
        _ = self.enterContext(mock.patch.object(pushover, "LOG_FILE", folder / "log"))
        _ = self.enterContext(mock.patch.object(pushover, "parent_source", return_value="python test_pushover.py"))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"NOTIFY_STATE_DIR": str(folder / "state")})))
        _ = self.enterContext(mock.patch.object(review, "review", side_effect=approve))
        _ = self.enterContext(mock.patch.object(review, "schedule"))
        _ = self.enterContext(mock.patch.object(pushover, "request_template_fix"))
        _ = self.enterContext(mock.patch.object(review, "run_process", side_effect=AssertionError("real reviewer")))
        _ = self.enterContext(mock.patch.object(pushover, "http_post", side_effect=AssertionError("real post")))

    def run_send(self, argv: list[str], status: int = 200, body: bytes = b'{"status":1}') -> tuple[int, dict[str, list[str]]]:
        sent: list[bytes] = []

        def post(_url: str, data: bytes) -> tuple[int, bytes]:
            sent.append(data)
            return status, body

        code = pushover.send(argv, self.keys, post)
        return code, urllib.parse.parse_qs(sent[0].decode()) if sent else {}

    def test_catalog_session_reaches_the_post_and_review_record(self) -> None:
        entry = push_texts.PushText("test", "shutdown", "decision", "Owner is blocked",
            push_texts.ActionTemplate("stop Owner"), "Reason: waiting.",
            (push_texts.PushExample("typical", {}),), purpose="Test.", session=push_texts.SessionTemplate("Owner"))
        with mock.patch.object(push_texts, "catalog", return_value=(entry,)), redirect_stdout(io.StringIO()) as output:
            code, form = self.run_send(["--text-id", "test"])
        self.assertEqual(code, 0)
        self.assertEqual(form["message"], [footed("Session: Owner\nAction: stop Owner\nReason: waiting.", "shutdown", "test")])
        self.assertIn('"session": "Owner"', output.getvalue())

    def test_unit_suffix_leaves_per_unit_and_other_free_text_as_written(self) -> None:
        body = "Session: style-unit\nCounts are per-unit now. Read the trunk-unit plan first."
        code, form = self.run_send(["--action", "open the herdr-unit pane", "messaging-unit stopped", body])
        self.assertEqual(code, 0)
        self.assertEqual(form["title"], ["messaging-unit stopped"])
        self.assertEqual(form["message"], [footed("Session: style-unit\nAction: open the herdr-unit pane\n"
                                                  + "Counts are per-unit now. Read the trunk-unit plan first.")])
        entry = push_texts.PushText("test", "{sender}", "decision", "{sender}: {session} is blocked",
            push_texts.ActionTemplate("stop {session}"), "Reason: {why}. Next: {next}.",
            (push_texts.PushExample("typical", {"sender": "x", "session": "y", "why": "z", "next": "w"}),),
            purpose="Test.", session=push_texts.SessionTemplate("{session}"))
        pushover.LOG_FILE.unlink()
        with mock.patch.object(push_texts, "catalog", return_value=(entry,)), redirect_stdout(io.StringIO()):
            code, form = self.run_send(["--text-id", "test", "--value", "sender=cargo-liner-unit", "--value", "session=types-unit",
                                        "--value", "why=counts are per-unit and the trunk-unit plan waits", "--value", "next=style-unit"])
        self.assertEqual(code, 0)
        self.assertEqual(form["title"], ["cargo-liner: types is blocked"])
        self.assertEqual(form["message"], [footed("Session: types\nAction: stop types\n"
                                                  + "Reason: counts are per-unit and the trunk-unit plan waits. Next: style.",
                                                  "cargo-liner", "test")])

    def test_unit_suffix_drops_only_from_a_session_value(self) -> None:
        for key, value, shown in (("session", "types-unit", "types"), ("sender", "cargo-liner-unit", "cargo-liner"),
                                  ("next", "style-unit", "style"), ("why", "per-unit counts", "per-unit counts"),
                                  ("why", "the trunk-unit plan", "the trunk-unit plan"), ("path", "/a/style-unit", "/a/style-unit"),
                                  ("session", "x-unit-test", "x-unit-test"), ("why", "a unit", "a unit")):
            with self.subTest(key=key, value=value):
                self.assertEqual(push_texts.shown_value(key, value), shown)

    def test_session_header_counts_toward_the_post_limit_without_truncation(self) -> None:
        code, form = self.run_send(["--action", "stop Owner", "Owner is blocked", "Session: Owner\n" + "x" * 1200])
        self.assertEqual(code, 0)
        message = form["message"][0]
        self.assertEqual(len(message), pushover.MESSAGE_MAX)
        self.assertTrue(message.startswith("Session: Owner\nAction: stop Owner\n"))

    def test_session_cannot_push_the_action_past_the_message_limit(self) -> None:
        with redirect_stderr(io.StringIO()):
            code, form = self.run_send(["--action", "x" * pushover.ACTION_TEXT_MAX,
                "Owner is blocked", "Session: Owner\nReason: waiting."])
        self.assertEqual(code, 2)
        self.assertEqual(form, {})

    def log_lines(self) -> list[dict[str, object]]:
        return [cast(dict[str, object], json.loads(line)) for line in pushover.LOG_FILE.read_text().splitlines()]

    def assert_log(self, priority: str, title: str, message: str, outcome: str,
                   source: str = "python test_pushover.py") -> None:
        entries = self.log_lines()
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(set(entry), {"time", "source", "priority", "title", "message", "outcome"})
        self.assertEqual((entry["source"], entry["priority"], entry["title"], entry["message"], entry["outcome"]),
                         (source, priority, title, message, outcome))
        stamp = datetime.fromisoformat(str(entry["time"]))
        self.assertIsNotNone(stamp.utcoffset())
        self.assertEqual(stamp.microsecond, 0)
        self.assertNotIn("u123", pushover.LOG_FILE.read_text())
        self.assertNotIn("a456", pushover.LOG_FILE.read_text())

    def test_normal_send_carries_keys_title_and_message(self) -> None:
        code, form = self.run_send(["--no-action", "Hana: unit", "merged"])
        self.assertEqual(code, 0)
        self.assertEqual(form["user"], ["u123"])
        self.assertEqual(form["token"], ["a456"])
        self.assertEqual(form["priority"], ["0"])
        self.assertEqual(form["message"], [footed("No action needed.\nmerged")])
        self.assertNotIn("retry", form)

    def test_emergency_repeats_until_acknowledged(self) -> None:
        code, form = self.run_send(["--priority", "2", "--action", "run github-warmup", "Hana: blocked",
                                    "GitHub credentials are cold."], body=b'{"status":1,"receipt":"r1"}')
        self.assertEqual(code, 0)
        self.assertEqual(form["retry"], [str(pushover.RETRY_S)])
        self.assertEqual(form["expire"], [str(pushover.EXPIRE_S)])
        posted = footed("Action: run github-warmup\nGitHub credentials are cold.")
        self.assertEqual(form["message"], [posted])
        self.assert_log("2", "Hana: blocked", posted, "sent, receipt r1")

    def test_keyed_emergency_carries_the_tag_of_its_key(self) -> None:
        code, form = self.run_send(["--priority", "2", "--key", "handler-prompt", "--action", "answer the prompt",
                                    "handler needs your answer", "The prompt waits."])
        self.assertEqual(code, 0)
        self.assertEqual(form["tags"], [pushover.push_tag("handler-prompt")])
        self.assertRegex(pushover.push_tag("handler prompt/1"), r"^[A-Za-z0-9_-]+$")
        self.assertEqual(pushover.push_tag("a"), pushover.push_tag("a"))
        self.assertNotEqual(pushover.push_tag("a"), pushover.push_tag("b"))

    def test_no_tag_without_a_key_or_below_emergency(self) -> None:
        for argv in (["--priority", "2", "--action", "check the UPS", "Power is out", "The UPS is on battery."],
                     ["--priority", "1", "--key", "k", "--action", "choose a branch", "Build stopped", "Choose."],
                     ["--key", "k", "--no-action", "Build done", "The build passed."]):
            with self.subTest(argv=argv), redirect_stdout(io.StringIO()):
                code, form = self.run_send(argv)
            self.assertEqual(code, 0)
            self.assertNotIn("tags", form)

    def run_cancel(self, argv: list[str], status: int = 200,
                   body: bytes = b'{"status":1,"canceled":1}') -> tuple[int, list[tuple[str, dict[str, list[str]]]], str]:
        posted: list[tuple[str, dict[str, list[str]]]] = []

        def post(url: str, data: bytes) -> tuple[int, bytes]:
            posted.append((url, urllib.parse.parse_qs(data.decode())))
            return status, body

        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()):
            code = pushover.cancel(argv, self.keys, post)
        return code, posted, output.getvalue()

    def test_cancel_posts_cancel_by_tag_with_the_token_and_logs(self) -> None:
        code, posted, output = self.run_cancel(["--source", "stall_watch", "handler-prompt"])
        tag = pushover.push_tag("handler-prompt")
        self.assertEqual(code, 0)
        self.assertEqual(posted, [(f"https://api.pushover.net/1/receipts/cancel_by_tag/{tag}.json", {"token": ["a456"]})])
        self.assertEqual(output, "pushover: cancelled 1\n")
        self.assert_log("2", "cancel emergency push", f"key handler-prompt, tag {tag}", "cancel: cancelled 1",
                        source="stall_watch")

    def test_cancel_with_nothing_repeating_says_so(self) -> None:
        code, _posted, output = self.run_cancel(["k"], body=b'{"status":1,"canceled":0}')
        self.assertEqual((code, output), (0, "pushover: nothing to cancel\n"))
        self.assertEqual(self.log_lines()[-1]["outcome"], "cancel: nothing to cancel")

    def test_cancel_failure_exits_one_and_bad_usage_two(self) -> None:
        code, _posted, _output = self.run_cancel(["k"], status=400, body=b'{"status":0,"errors":["application token is invalid"]}')
        self.assertEqual(code, 1)
        self.assertEqual(self.log_lines()[-1]["outcome"], "cancel: refused: application token is invalid")
        for argv in ([], ["--source", "x"], ["a", "b"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.run_cancel(argv)[:2], (2, []))
        _ = self.keys.write_text("PUSHOVER_USER=u123\n")
        self.assertEqual(self.run_cancel(["k"])[:2], (2, []))

    def test_missing_keys_send_nothing(self) -> None:
        _ = self.keys.write_text("PUSHOVER_USER=u123\nPUSHOVER_TOKEN=\n")
        code, form = self.run_send(["--no-action", "t", "m"])
        self.assertEqual((code, form), (2, {}))
        self.assert_log("0", "t", footed("No action needed.\nm"), "keys missing")

    def test_bad_usage_sends_nothing(self) -> None:
        for argv in (["--priority", "3", "t", "m"], ["only title"], ["t", " "]):
            self.assertEqual(self.run_send(argv), (2, {}))
        self.assertFalse(pushover.LOG_FILE.exists())

    def test_refusal_reports_pushover_errors(self) -> None:
        code, _ = self.run_send(["--no-action", "t", "m"], status=400,
                                body=b'{"status":0,"errors":["application token is invalid"]}')
        self.assertEqual(code, 1)
        self.assert_log("0", "t", footed("No action needed.\nm"), "refused: application token is invalid")

    def test_array_reply_logs_http_refusal(self) -> None:
        code, _ = self.run_send(["--no-action", "t", "m"], body=b"[]")
        self.assertEqual(code, 1)
        self.assert_log("0", "t", footed("No action needed.\nm"), "refused: HTTP 200")

    def test_null_reply_logs_http_refusal(self) -> None:
        code, _ = self.run_send(["--no-action", "t", "m"], body=b"null")
        self.assertEqual(code, 1)
        self.assert_log("0", "t", footed("No action needed.\nm"), "refused: HTTP 200")

    def test_sent_log_keeps_multiline_text_after_limits(self) -> None:
        message = "first line\nsecond line"
        code, form = self.run_send(["--no-action", "Hana: unit", message])
        self.assertEqual(code, 0)
        posted = footed("No action needed.\n" + message)
        self.assertEqual(form["message"], [posted])
        self.assert_log("0", "Hana: unit", posted, "sent")

    def test_overlong_title_and_message_log_the_posted_values(self) -> None:
        title = "T" * (pushover.TITLE_MAX + 3)
        message = "M" * (pushover.MESSAGE_MAX + 3)
        code, form = self.run_send(["--no-action", title, message])
        self.assertEqual(code, 0)
        self.assertEqual(form["title"], [title[:pushover.TITLE_MAX]])
        footer = footed("")
        posted = ("No action needed.\n" + message)[:pushover.MESSAGE_MAX - len(footer)] + footer
        self.assertEqual(len(posted), pushover.MESSAGE_MAX)
        self.assertEqual(form["message"], [posted])
        self.assert_log("0", title[:pushover.TITLE_MAX], posted, "sent")

    def test_unreachable_log_keeps_text(self) -> None:
        def unavailable(_url: str, _data: bytes) -> tuple[int, bytes]:
            raise urllib.error.URLError("offline")

        with mock.patch("sys.stderr"):
            code = pushover.send(["--no-action", "t", "first\nsecond"], self.keys, unavailable)
        self.assertEqual(code, 1)
        self.assert_log("0", "t", footed("No action needed.\nfirst\nsecond"), "unreachable: <urlopen error offline>")

    def test_unwritable_log_does_not_change_exit_status(self) -> None:
        folder = pushover.LOG_FILE.parent / "directory"
        folder.mkdir()
        errors = io.StringIO()
        with (mock.patch.object(pushover, "LOG_FILE", folder),
              redirect_stderr(errors)):
            code, _ = self.run_send(["--no-action", "t", "m"])
        self.assertEqual(code, 0)
        self.assertEqual(len(errors.getvalue().splitlines()), 1)

    def test_unstated_action_is_refused_before_posting_and_logged(self) -> None:
        errors = io.StringIO()
        with redirect_stderr(errors):
            code, form = self.run_send(["--source", "legacy caller", "legacy title", "legacy body"])
        self.assertEqual((code, form), (2, {}))
        self.assertEqual(
            errors.getvalue(),
            f"pushover: refused before sending: no action stated. {FIX}\n",
        )
        self.assert_log(
            "0",
            "legacy title",
            "legacy body",
            "refused before sending: no action stated",
            "legacy caller",
        )

    def test_wrong_action_is_refused_before_posting_and_logged(self) -> None:
        errors = io.StringIO()
        with redirect_stderr(errors):
            code, form = self.run_send(["--source", "live check", "--action", "none", "title", "body"])
        self.assertEqual((code, form), (2, {}))
        self.assertIn("refused before sending", errors.getvalue())
        self.assertIn(FIX, errors.getvalue())
        outcome = 'refused before sending: --action "none" does not state an action; ' + "use --no-action"
        self.assert_log("0", "title", "body", outcome, "live check")

    def test_action_free_emergency_is_refused(self) -> None:
        code, form = self.run_send(["--priority", "2", "--no-action", "title", "body"])
        self.assertEqual((code, form), (2, {}))
        self.assert_log("2", "title", "body", "refused before sending: priority 2 requires --action")

    def test_cut_keeps_the_action_line_and_cuts_only_the_body(self) -> None:
        body = "B" * (pushover.MESSAGE_MAX + 20)
        _, form = self.run_send(["--action", "restart the service", "title", body])
        posted = form["message"][0]
        self.assertEqual(len(posted), pushover.MESSAGE_MAX)
        self.assertTrue(posted.startswith("Action: restart the service\n"))
        self.assertTrue(posted.endswith("B\n\nSent by: python test_pushover.py\nTemplate: none (free text)"))

    def test_action_too_long_for_its_line_is_refused_without_posting(self) -> None:
        action = "A" * pushover.MESSAGE_MAX
        errors = io.StringIO()
        with redirect_stderr(errors):
            code, form = self.run_send(["--action", action, "title", "body"])
        self.assertEqual((code, form), (2, {}))
        reason = f"--action may hold at most {pushover.ACTION_TEXT_MAX} characters"
        self.assertIn(reason, errors.getvalue())
        self.assert_log("0", "title", "body", f"refused before sending: {reason}")

    def test_source_option_and_parent_command_are_logged(self) -> None:
        _ = self.run_send(["--source", "watcher", "--no-action", "explicit", "body"])
        self.assertEqual(self.log_lines()[0]["source"], "watcher")
        pushover.LOG_FILE.unlink()
        _ = self.run_send(["--no-action", "inferred", "body"])
        self.assertEqual(self.log_lines()[0]["source"], "python test_pushover.py")

    def assert_footer(self, message: str, sender: str, template: str) -> None:
        self.assertEqual(message.splitlines()[-3:], ["", f"Sent by: {sender}", f"Template: {template}"])

    def test_every_post_ends_with_its_sender_then_its_template(self) -> None:
        entry = next(item for item in push_texts.catalog() if item.id == "nixos.ups_battery")
        registered = ["--text-id", entry.id, *(item for key, value in entry.examples[0].values.items()
                                                for item in ("--value", f"{key}={value}"))]
        for argv, sender, template in (
                (registered, "ups", "nixos.ups_battery"),
                (["--no-action", "Build done", "The build passed."], PARENT, "none (free text)"),
                (["--priority", "2", "--action", "run github-warmup", "GitHub keys cold", "No pushes."], PARENT, "none (free text)"),
                (["--source", "send.py in hana", "--priority", "2", "--action", "run github-warmup", "GitHub keys cold",
                  "No pushes."], "hana", "none (free text)"),
                (["--source", "stall-watch", "--no-action", "Unit idle", "The unit waits."], "stall-watch", "none (free text)")):
            with self.subTest(sender=sender, template=template, argv=argv[:3]), redirect_stdout(io.StringIO()):
                code, form = self.run_send(argv)
            self.assertEqual(code, 0)
            self.assert_footer(form["message"][0], sender, template)
            self.assertEqual(self.log_lines()[-1]["message"], form["message"][0])

    def test_a_push_released_from_silence_keeps_its_sender_and_template(self) -> None:
        entry = push_texts.PushText("test.kept", "build-followups", "note", "Build done", push_texts.NoActionNeeded(),
                                   "The build passed.", (push_texts.PushExample("typical", {}),), purpose="Test.")
        self.silence_on()
        with mock.patch.object(push_texts, "catalog", return_value=(entry,)), redirect_stdout(io.StringIO()):
            self.assertEqual(self.run_send(["--text-id", "test.kept"]), (0, {}))
        self.assertEqual(self.log_lines()[-1]["message"], "No action needed.\nThe build passed.")
        posted: list[str] = []

        def post(_url: str, data: bytes) -> tuple[int, bytes]:
            posted.append(urllib.parse.parse_qs(data.decode())["message"][0])
            return 200, b'{"status":1}'

        with redirect_stdout(io.StringIO()):
            self.assertEqual(silence.off(lambda _argv: 0, lambda push: pushover.post_kept(push, self.keys, post)), 0)
        self.assertEqual(posted, [footed("No action needed.\nThe build passed.", "build-followups", "test.kept")])

    def test_an_empty_sender_is_refused_and_nothing_is_posted(self) -> None:
        for source in ("", "  ", "send.py in "):
            with self.subTest(source=source), redirect_stderr(io.StringIO()) as errors:
                code = pushover.post_message(source, "2", "t", "Action: run it\nm", self.keys,
                                             mock.Mock(side_effect=AssertionError("posted without a sender")), "")
            self.assertEqual(code, 2)
            self.assertIn("refused before sending: no sender", errors.getvalue())
            self.assertEqual(self.log_lines()[-1]["outcome"], "refused before sending: no sender")

    def test_a_long_body_is_cut_so_the_footer_fits(self) -> None:
        sent: list[str] = []

        def post(_url: str, data: bytes) -> tuple[int, bytes]:
            sent.append(urllib.parse.parse_qs(data.decode())["message"][0])
            return 200, b'{"status":1}'

        with redirect_stdout(io.StringIO()):
            code = pushover.post_message("send.py in hana", "0", "t", "No action needed.\n" + "B" * 2000, self.keys, post,
                                         "", text_id="auto_mode.refused")
        self.assertEqual(code, 0)
        self.assertEqual(len(sent[0]), pushover.MESSAGE_MAX)
        self.assertTrue(sent[0].startswith("No action needed.\nBBB"))
        self.assert_footer(sent[0], "hana", "auto_mode.refused")

    def draft(self, *, answerable: bool = False) -> review.Draft:
        return review.Draft("writer", "1", "Build stopped", ActionRequired("Choose in the natedev terminal."),
                            "Choose whether to retry the build.", answerable)

    def arguments(self, *, answerable: bool = False) -> list[str]:
        draft = self.draft(answerable=answerable)
        assert isinstance(draft.action, ActionRequired)
        return ["--source", draft.source, "--priority", draft.priority, "--action", draft.action.text,
                *(["--answerable"] if answerable else []), draft.title, draft.body]

    def reviews(self) -> list[dict[str, object]]:
        path = self.keys.parent / "state/review.jsonl"
        return [cast(dict[str, object], json.loads(line)) for line in path.read_text().splitlines()]

    def test_rewrite_posts_exact_version_and_prints_all_sent_text(self) -> None:
        draft = self.draft()
        version = replace(draft, title="Build needs your decision", body="Retry or stop in the natedev terminal.")
        with (mock.patch.object(review, "review", return_value=review.Rewritten(version, ["reader-starts-cold"])) as read,
              redirect_stdout(io.StringIO()) as output):
            code, form = self.run_send(self.arguments())
        self.assertEqual(code, 0)
        read.assert_called_once_with(draft, revision=False)
        self.assertEqual(form["title"], [version.title])
        self.assertEqual(form["message"], [footed(first_line(version.action) + "\n" + version.body, draft.source)])
        self.assertEqual(form["priority"], ["1"])
        self.assertIn("pushover: sent as rewritten:\n" + version.title + "\n" +
                      first_line(version.action) + "\n" + version.body + "\n", output.getvalue())
        self.assertEqual(self.reviews()[0]["verdict"], "rewrite")
        self.assertEqual(cast(dict[str, object], self.reviews()[0]["sent"])["body"], version.body)

    def test_failed_delivery_prints_the_exact_reviewed_fallback(self) -> None:
        draft = self.draft()
        version = replace(draft, title="Build needs your decision", action=ActionRequired("Choose retry.\nUse natedev."),
                          body="The build stopped.\nChoose retry or stop.")
        for failure in ("post", "keys", "timeout"):
            with (self.subTest(failure=failure), mock.patch.object(review, "review", return_value=review.Rewritten(version, [])),
                  redirect_stdout(io.StringIO()) as printed, redirect_stderr(io.StringIO())):
                if failure == "keys":
                    code = pushover.send(self.arguments(), self.keys.parent / "missing", lambda _u, _d: (200, b'{"status":1}'))
                elif failure == "timeout":
                    def post(_url: str, _data: bytes) -> tuple[int, bytes]:
                        raise TimeoutError("post timed out")
                    code = pushover.send(self.arguments(), self.keys, post)
                else:
                    code, _ = self.run_send(self.arguments(), status=500, body=b'{"status":0}')
            self.assertIn(code, (1, 2))
            text = printed.getvalue()
            self.assertIn("pushover: reviewed JSON: " + json.dumps(review.message_record(version)) + "\n", text)
            self.assertIn(version.body.replace("\n", "\\n"), text)

    def test_failed_revision_prints_its_restored_hold_source(self) -> None:
        """F004: send.py needs the held source before posting can fail."""
        draft = self.draft(answerable=True)
        held = review.Held(draft.source, 1000, draft, draft, ["Which build stopped?"])
        review.hold(held)
        with redirect_stdout(io.StringIO()) as output:
            code, _ = self.run_send(self.arguments(answerable=True), status=500, body=b'{"status":0}')
        self.assertEqual(code, 1)
        line = output.getvalue().splitlines()[0]
        record = cast(dict[str, object], json.loads(line.removeprefix("pushover: reviewed JSON: ")))
        self.assertEqual(record["held_source"], draft.source)
        self.assertEqual(record["held_identity"], review.hold_identity(held))
        self.assertIsInstance(review.held_for(draft.source), review.LiveHold)

    def test_session_ask_never_reads_keys_or_schedules_expiry_delivery(self) -> None:
        draft = self.draft(answerable=True)
        questions = review.Questions(draft, ["Which build stopped?"], ["reader-starts-cold"])
        with (mock.patch.object(review, "review", return_value=questions),
              mock.patch.object(pushover, "read_keys", side_effect=AssertionError("held read keys")),
              redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(self.run_send(self.arguments(answerable=True)), (6, {}))
        self.assertIn("not sent: reader-starts-cold", errors.getvalue())
        self.assertIsInstance(review.held_for(draft.source), review.NoHold)
        self.assertIsNone(self.reviews()[0]["sent"])

    def test_failed_revision_is_retired_and_cannot_post_on_expiry(self) -> None:
        draft = self.draft(answerable=True)
        held = review.Held(draft.source, 1000, draft, draft, ["Which build stopped?"])
        review.hold(held)
        revised = replace(draft, body="The nightly build stopped. Choose whether to retry.")
        replies = iter((verdict(draft, "rewrite", body=revised.body, failed_rules=[{"rule": "reader-starts-cold", "quote": draft.body}]),
                        verdict(revised, "rewrite", body="Choose retry.", failed_rules=[{"rule": "reader-starts-cold", "quote": revised.body}])))
        def runner(argv: list[str], *, timeout: float, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
            value = next(replies) if Path(argv[0]).name == "claude" else verdict(draft)
            return FakeReviewer(value)(argv, timeout=timeout, env=env)
        def read(draft: review.Draft, revision: bool) -> review.ReviewOutcome:
            return REAL_REVIEW(draft, revision, runner=runner)
        with (mock.patch.object(review, "GUIDE", self.keys.parent),
              mock.patch.object(review, "reviewer_environment", return_value=({}, "default")),
              mock.patch.object(review, "review", side_effect=read)):
            self.assertEqual(self.run_send(self.arguments(answerable=True)), (6, {}))
        with mock.patch.object(time, "time", return_value=time.time() + review.claim_stale_seconds() + 600):
            self.assertEqual(review.due(post=mock.Mock(side_effect=AssertionError("retired claim posted")), keys_file=self.keys), 0)
        self.assertIsInstance(review.held_for(draft.source), review.NoHold)
        self.assertIsNone(self.reviews()[0]["sent"])
        self.assertEqual(review.held_path(draft.source).with_suffix(".held").read_text(), review.hold_identity(held))

    def test_held_result_is_published_only_after_its_claim_is_retired(self) -> None:
        draft = self.draft(answerable=True)
        held = review.Held(draft.source, 1000, draft, draft, ["Which build stopped?"])
        review.hold(held)
        retire = review.retire_claim
        with redirect_stdout(io.StringIO()) as output:
            def retire_before_output(held: review.Held) -> None:
                self.assertNotIn("pushover: reviewed JSON:", output.getvalue())
                retire(held)
            with (mock.patch.object(review, "review", return_value=review.Failed(draft, ["reader-starts-cold"])),
                  mock.patch.object(review, "retire_claim", side_effect=retire_before_output)):
                self.assertEqual(self.run_send(self.arguments(answerable=True)), (6, {}))
        self.assertIn('"held": true', output.getvalue())

    def test_withheld_session_note_is_not_posted_but_approved_finished_plan_note_is(self) -> None:
        with mock.patch.object(review, "review", return_value=review.Withheld(self.draft(), "No user action or timely information.")):
            self.assertEqual(self.run_send(["--no-action", "Still working", "The tests are running."]), (6, {}))
        self.assertEqual(self.run_send(["--no-action", "Plan finished", "The whole plan finished and merged."])[0], 0)

    def test_blocked_review_failure_is_posted_and_reported(self) -> None:
        draft = self.draft()
        failed = review.Failed(replace(draft, priority="2"), ["reader-starts-cold"])
        with (mock.patch.object(review, "review", return_value=failed), redirect_stdout(io.StringIO()) as output):
            code, form = self.run_send(["--priority", "2", "--action", "Choose retry.", draft.title, draft.body])
        self.assertEqual(code, 0)
        self.assertEqual(form["priority"], ["2"])
        self.assertIn("sent despite failed review: reader-starts-cold", output.getvalue())
        self.assertIn("sent despite failed review", str(self.reviews()[0]["detail"]))

    def test_registry_review_failure_sends_rendered_text_and_logs_its_id(self) -> None:
        rendered = push_texts.RenderedPush("test", "script", "decision", "Choose retry", ActionRequired("Choose retry."), "The build stopped.", {})
        wrong = review.Draft("script", "1", "Different title", rendered.action, "Different body", False)
        with (mock.patch.object(push_texts, "render", return_value=rendered),
              mock.patch.object(review, "held_for", side_effect=AssertionError("registry push used session hold")),
              mock.patch.object(pushover, "request_template_fix") as fix,
              mock.patch.object(review, "review", return_value=review.Failed(wrong, ["reader-starts-cold"]))):
            code, form = self.run_send(["--text-id", "test"])
        self.assertEqual(code, 0)
        self.assertEqual(form["title"], [rendered.title])
        self.assertEqual(form["message"], [footed(first_line(rendered.action) + "\n" + rendered.body, "script", "test")])
        self.assertEqual(self.reviews()[0]["text_id"], "test")
        self.assertIn("sent despite failed review", str(self.reviews()[0]["detail"]))
        fix.assert_called_once()
        record = cast(pushover.ReviewedPushRecord, fix.call_args.args[0])
        self.assertEqual(record.get("text_id"), "test")
        self.assertEqual(record.get("failed_rules"), ["reader-starts-cold"])

    def test_registry_rewrite_is_logged_but_the_rendered_text_is_sent(self) -> None:
        rendered = push_texts.RenderedPush("test", "script", "decision", "Choose retry", ActionRequired("Choose retry."), "The build stopped.", {})
        rewrite = review.Draft("script", "1", "Different title", rendered.action, "Different body", False)
        with (mock.patch.object(push_texts, "render", return_value=rendered),
              mock.patch.object(review, "review", return_value=review.Rewritten(rewrite, ["reader-starts-cold"])),
              redirect_stdout(io.StringIO()) as output):
            code, form = self.run_send(["--text-id", "test"])
        self.assertEqual(code, 0)
        self.assertEqual(form["title"], [rendered.title])
        self.assertEqual(form["message"], [footed(first_line(rendered.action) + "\n" + rendered.body, "script", "test")])
        self.assertNotIn("sent as rewritten", output.getvalue())
        self.assertEqual(self.reviews()[0]["verdict"], "rewrite")
        self.assertEqual(cast(dict[str, object], self.reviews()[0]["sent"])["body"], rendered.body)

    def test_approved_registered_text_skips_the_live_review(self) -> None:
        approved = push_texts.approval_date("nixos.ups_battery")
        self.assertRegex(approved, r"^\d{4}-\d{2}-\d{2}$")
        entry = next(item for item in push_texts.catalog() if item.id == "nixos.ups_battery")
        values = entry.examples[0].values
        argv = ["--text-id", entry.id, *(item for key, value in values.items() for item in ("--value", f"{key}={value}"))]
        with (mock.patch.object(review, "review", side_effect=AssertionError("live review of an approved text")),
              redirect_stdout(io.StringIO()) as output):
            code, form = self.run_send(argv)
        self.assertEqual(code, 0)
        self.assertEqual(form["priority"], ["2"])
        detail = f"registered text, approved {approved}"
        rendered = push_texts.render_entry(entry, values)
        self.assertEqual(form["title"], [rendered.title])
        self.assertEqual(form["message"], [footed(rendered.message, rendered.sender, entry.id)])
        self.assertEqual(self.reviews()[0]["detail"], detail)
        self.assertEqual(self.log_lines()[-1]["outcome"], "sent, " + detail)
        self.assertNotIn("failed review", output.getvalue())

    def test_unapproved_registered_text_is_still_reviewed(self) -> None:
        entry = push_texts.PushText("test", "script", "decision", "Choose retry", push_texts.ActionTemplate("Choose retry."),
                                   "The build stopped.", (push_texts.PushExample("typical", {}),), purpose="Test.")
        with (mock.patch.object(push_texts, "catalog", return_value=(entry,)),
              mock.patch.object(review, "review", side_effect=approve) as read,
              redirect_stdout(io.StringIO())):
            code, _form = self.run_send(["--text-id", "test"])
        self.assertEqual(code, 0)
        read.assert_called_once()
        self.assertEqual(push_texts.approval_date("test"), "")

    def adhoc(self) -> tuple[review.Draft, list[str]]:
        description = "You asked to be told when the /nate skill is ready. It is ready now."
        values = {"sender": "natedev", "title": "the /nate skill is ready", "description": description}
        argv = ["--text-id", "nate.adhoc", *(item for key, value in values.items() for item in ("--value", f"{key}={value}"))]
        draft = review.Draft("natedev", "0", "natedev the /nate skill is ready", NoActionRequired(), description, False)
        return draft, argv

    def test_approved_nate_adhoc_runs_live_review_and_keeps_its_template_footer(self) -> None:
        draft, argv = self.adhoc()
        self.assertRegex(push_texts.approval_date("nate.adhoc"), r"^\d{4}-\d{2}-\d{2}$")
        with (mock.patch.object(review, "review", side_effect=approve) as read,
              redirect_stdout(io.StringIO()) as output):
            code, form = self.run_send(argv)
        read.assert_called_once_with(draft, revision=False)
        self.assertEqual(code, 0)
        self.assertEqual(form["title"], [draft.title])
        self.assertEqual(form["message"], [footed(draft.body, draft.source, "nate.adhoc")])
        self.assertIn('"text_id": "nate.adhoc"', output.getvalue())
        self.assertEqual(self.reviews()[0]["text_id"], "nate.adhoc")
        self.assertEqual(self.reviews()[0]["verdict"], "send")

    def test_failed_nate_adhoc_is_held_and_records_its_text_id(self) -> None:
        draft, argv = self.adhoc()
        for outcome in (review.Failed(draft, ["reader-starts-cold"]), review.Withheld(draft, "No timely information.")):
            with (self.subTest(outcome=type(outcome).__name__),
                  mock.patch.object(review, "review", return_value=outcome),
                  mock.patch.object(pushover, "read_keys", side_effect=AssertionError("held push read keys")),
                  mock.patch.object(pushover, "request_template_fix") as fix,
                  redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors):
                self.assertEqual(self.run_send(argv), (6, {}))
            record = cast(pushover.ReviewedPushRecord, json.loads(output.getvalue().splitlines()[0].removeprefix("pushover: reviewed JSON: ")))
            self.assertIs(record.get("held"), True)
            self.assertEqual(record.get("text_id"), "nate.adhoc")
            self.assertIn("pushover: not sent: ", errors.getvalue())
            self.assertEqual(self.reviews()[-1]["text_id"], "nate.adhoc")
            self.assertIsNone(self.reviews()[-1]["sent"])
            fix.assert_not_called()

    def test_nate_adhoc_sends_and_reports_the_reviewers_rewrite(self) -> None:
        draft, argv = self.adhoc()
        rewrite = replace(draft, title="The /nate skill is ready", body="Rewritten body")
        with (mock.patch.object(review, "review", return_value=review.Rewritten(rewrite, ["reader-starts-cold"])),
              redirect_stdout(io.StringIO()) as output):
            code, form = self.run_send(argv)
        self.assertEqual(code, 0)
        self.assertEqual(form["title"], [rewrite.title])
        self.assertEqual(form["message"], [footed(rewrite.body, draft.source, "nate.adhoc")])
        self.assertIn("pushover: sent as rewritten:\n" + rewrite.title, output.getvalue())
        self.assertIn(rewrite.body, output.getvalue())
        self.assertEqual(self.reviews()[0]["text_id"], "nate.adhoc")
        self.assertEqual(cast(dict[str, object], self.reviews()[0]["sent"])["body"], rewrite.body)

    def test_nate_adhoc_looks_up_and_claims_its_sources_hold(self) -> None:
        draft, argv = self.adhoc()
        held = review.Held(draft.source, 1000, draft, draft, ["Which update?"])
        review.hold(held)
        with (mock.patch.object(review, "held_for", wraps=review.held_for) as lookup,
              mock.patch.object(review, "review", side_effect=approve) as read,
              redirect_stdout(io.StringIO()) as output):
            code, _form = self.run_send(argv)
        self.assertEqual(code, 0)
        self.assertEqual(lookup.call_args_list, [mock.call(draft.source), mock.call(draft.source)])
        read.assert_called_once_with(draft, revision=True)
        self.assertIn('"held_source": "natedev"', output.getvalue())

    def test_cancelled_revision_drops_its_claim_without_posting_or_reporting_a_rewrite(self) -> None:
        draft = self.draft(answerable=True)
        held = review.Held(draft.source, 1000, draft, draft, ["Which build stopped?"])
        review.hold(held)

        def read(revised: review.Draft, revision: bool) -> review.ReviewOutcome:
            self.assertTrue(revision)
            # The reviewer resumes after the gate has cancelled its stale claim.
            sending = review.held_path(held.source).with_suffix(".sending")
            stamp = time.time() - review.claim_stale_seconds()
            os.utime(sending, (stamp, stamp))
            self.assertEqual(review.release_if_matches(held.source, review.hold_identity(held)),
                             review.ReleaseOutcome.RELEASED)
            return review.Rewritten(replace(revised, body="Choose whether to retry the nightly build."),
                                    ["reader-starts-cold"])

        with (mock.patch.object(review, "review", side_effect=read),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(self.run_send(self.arguments(answerable=True)), (5, {}))
        self.assertIn("pushover: replaced: a newer version replaced this push; nothing was sent", output.getvalue())
        self.assertNotIn("pushover: sent", output.getvalue())
        self.assertNotIn("sent as rewritten:", output.getvalue())
        self.assertIsNone(self.reviews()[0]["sent"])
        self.assertEqual(self.reviews()[0]["detail"], "claim cancelled before posting")
        self.assertIsInstance(review.held_for(held.source), review.NoHold)

    def test_successful_revision_records_delivery_before_a_fallback_can_release_it(self) -> None:
        draft = self.draft(answerable=True)
        held = review.Held(draft.source, 1000, draft, draft, ["Which build stopped?"])
        review.hold(held)
        self.assertEqual(self.run_send(self.arguments(answerable=True))[0], 0)
        self.assertEqual(review.release_if_matches(held.source, review.hold_identity(held)),
                         review.ReleaseOutcome.DELIVERED)

    def test_failed_revision_restores_the_previous_hold(self) -> None:
        draft = self.draft(answerable=True)
        held = review.Held(draft.source, 1000, draft, draft, ["Which build stopped?"])
        review.hold(held)
        code, _ = self.run_send(self.arguments(answerable=True), status=500, body=b'{"status":0}')
        self.assertEqual(code, 1)
        found = review.held_for(draft.source)
        self.assertIsInstance(found, review.LiveHold)
        assert isinstance(found, review.LiveHold)
        self.assertEqual(found.hold, held)
        self.assertIsNone(self.reviews()[0]["sent"])

    def test_expiry_claim_prevents_a_revision_from_posting_twice(self) -> None:
        draft = self.draft(answerable=True)
        review.hold(review.Held(draft.source, 1000, draft, draft, ["Which build stopped?"]))
        self.assertTrue(review.claim(draft.source))
        with mock.patch.object(review, "review", side_effect=AssertionError("review during delivery")):
            self.assertEqual(self.run_send(self.arguments(answerable=True)), (4, {}))


    def test_switch_off_skips_reviewer_and_logs_original(self) -> None:
        with redirect_stdout(io.StringIO()):
            _ = review.switch("off")
        with mock.patch.object(review, "review", side_effect=AssertionError("review while off")):
            code, form = self.run_send(self.arguments())
        self.assertEqual(code, 0)
        self.assertEqual(form["message"], [footed(first_line(self.draft().action) + "\n" + self.draft().body, "writer")])
        self.assertIn("review: off", str(self.log_lines()[0]["outcome"]))

    def test_review_off_preserves_multiline_and_single_line_actions(self) -> None:
        with redirect_stdout(io.StringIO()):
            _ = review.switch("off")
        for action in ("Choose A\nor B", "Choose A or B"):
            for body in ("Choose in the natedev terminal.", "B" * 1100):
                with (self.subTest(action=action, body_length=len(body)),
                      mock.patch.object(review, "review", side_effect=AssertionError("review while off"))):
                    code, form = self.run_send(["--action", action, "Build stopped", body])
                expected_body = body[:1024 - len("Action: " + action + "\n")]
                footer = footed("")
                self.assertEqual(code, 0)
                self.assertEqual(form["message"], [("Action: " + action + "\n" + expected_body)[:1024 - len(footer)] + footer])
                self.assertEqual(cast(dict[str, object], self.reviews()[-1]["draft"])["body"], expected_body)

    def test_unreviewed_preserves_multiline_and_single_line_actions(self) -> None:
        for action in ("Choose A\nor B", "Choose A or B"):
            for body in ("Choose in the natedev terminal.", "B" * 1100):
                with (self.subTest(action=action, body_length=len(body)),
                      mock.patch.object(review, "review", return_value=review.Unreviewed("timeout"))):
                    code, form = self.run_send(["--action", action, "Build stopped", body])
                expected_body = body[:1024 - len("Action: " + action + "\n")]
                footer = footed("")
                self.assertEqual(code, 0)
                self.assertEqual(form["message"], [("Action: " + action + "\n" + expected_body)[:1024 - len(footer)] + footer])
                self.assertEqual(cast(dict[str, object], self.reviews()[-1]["draft"])["body"], expected_body)

    def test_action_refusal_happens_before_review(self) -> None:
        with mock.patch.object(review, "review", side_effect=AssertionError("review refused action")):
            self.assertEqual(self.run_send(["--action", "none", "title", "body"]), (2, {}))

    def test_unreviewed_failures_send_original_and_log_why(self) -> None:
        draft = self.draft()
        for why in ("timeout", "bad JSON", "reviewer exit 1", "no guide", "no quota"):
            with self.subTest(why=why), mock.patch.object(review, "review", return_value=review.Unreviewed(why)):
                code, form = self.run_send(self.arguments())
            self.assertEqual(code, 0)
            self.assertEqual(form["message"], [footed(first_line(draft.action) + "\n" + draft.body, "writer")])
            self.assertIn("unreviewed: " + why, str(self.log_lines()[-1]["outcome"]))
            self.assertEqual(self.reviews()[-1]["verdict"], "unreviewed: " + why)

    def test_configured_reviewer_timeout_still_posts_inside_the_derived_sender_wait(self) -> None:
        draft = self.draft()
        runner = FakeReviewer(verdict(draft))
        elapsed = [0.0]

        def process(argv: list[str], *, timeout: float, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
            if Path(argv[0]).name == "claude":
                self.assertLessEqual(timeout, 90)
                elapsed[0] += timeout
                raise subprocess.TimeoutExpired(argv, timeout)
            return runner(argv, timeout=timeout, env=env)

        def read(draft: review.Draft, revision: bool) -> review.ReviewOutcome:
            return REAL_REVIEW(draft, revision, runner=process)

        posted: list[bytes] = []

        def post(_url: str, data: bytes) -> tuple[int, bytes]:
            elapsed[0] += pushover.TIMEOUT_S
            posted.append(data)
            return 200, b'{"status":1}'

        with (mock.patch.object(review, "GUIDE", self.keys.parent),
              mock.patch.object(review, "reviewer_environment", return_value=({}, "default")),
              mock.patch.object(time, "monotonic", side_effect=lambda: elapsed[0]),
              mock.patch.object(review, "review", side_effect=read),
              mock.patch.object(review, "review_timeout_seconds", return_value=90)):
            self.assertEqual(pushover.send(self.arguments(), self.keys, post), 0)
            self.assertLess(elapsed[0], review.user_send_seconds())
        self.assertEqual(len(posted), 1)
        self.assertEqual(elapsed[0], 105)
        self.assertEqual(urllib.parse.parse_qs(posted[0].decode())["message"],
                         [footed(first_line(draft.action) + "\n" + draft.body, "writer")])
        self.assertIn("unreviewed: timeout", str(self.log_lines()[0]["outcome"]))

    def silence_on(self) -> None:
        with redirect_stdout(io.StringIO()):
            self.assertEqual(silence.on(None, lambda _argv: 1), 0)

    def test_silence_keeps_a_push_and_exits_zero_without_posting(self) -> None:
        self.silence_on()
        with redirect_stdout(io.StringIO()) as output:
            code, form = self.run_send(["--no-action", "Build done", "The build passed."])
        self.assertEqual((code, form), (0, {}))
        self.assertIn("pushover: kept: silence until off\n", output.getvalue())
        self.assertEqual([push["title"] for push in silence.read_kept()], ["Build done"])
        self.assertEqual(self.log_lines()[-1]["outcome"], "kept: silence until off")

    def test_key_reaches_the_kept_push_and_a_newer_push_with_it_replaces_the_older(self) -> None:
        self.silence_on()
        with redirect_stdout(io.StringIO()):
            for title in ("Old note", "New note"):
                code, _form = self.run_send(["--source", "hana", "--key", "subject", "--no-action", title, f"{title} body."])
                self.assertEqual(code, 0)
        self.assertEqual([(push["title"], push["key"]) for push in silence.read_kept()], [("New note", "subject")])

    def test_silence_never_keeps_a_blocked_push(self) -> None:
        self.silence_on()
        with redirect_stdout(io.StringIO()):
            code, form = self.run_send(["--priority", "2", "--action", "check the UPS", "Power is out", "The UPS is on battery."])
        self.assertEqual(code, 0)
        self.assertEqual(form["title"], ["Power is out"])
        self.assertEqual(silence.read_kept(), [])

    def test_silence_off_posts_the_kept_pushes_oldest_first_as_they_would_have_gone_out(self) -> None:
        self.silence_on()
        with redirect_stdout(io.StringIO()):
            for title in ("First", "Second"):
                _ = self.run_send(["--no-action", title, f"{title} body."])
        posted: list[dict[str, list[str]]] = []

        def post(_url: str, data: bytes) -> tuple[int, bytes]:
            posted.append(urllib.parse.parse_qs(data.decode()))
            return 200, b'{"status":1}'

        with redirect_stdout(io.StringIO()) as output:
            code = silence.off(lambda _argv: 0, lambda push: pushover.post_kept(push, self.keys, post))
        self.assertEqual(code, 0)
        self.assertEqual(output.getvalue(), "pushover: sent\n" * 2 + "silence off; 2 kept pushes delivered\n")
        self.assertEqual([(form["title"], form["message"]) for form in posted],
                         [(["First"], [footed("No action needed.\nFirst body.")]),
                          (["Second"], [footed("No action needed.\nSecond body.")])])
        self.assertEqual(self.log_lines()[-1]["outcome"], "sent, kept during silence")
        with redirect_stdout(io.StringIO()):
            code, form = self.run_send(["--no-action", "Third", "Third body."])
        self.assertEqual(form["title"], ["Third"])


    def test_a_push_after_a_missed_end_posts_the_kept_pushes_first(self) -> None:
        self.silence_on()
        with redirect_stdout(io.StringIO()):
            _ = self.run_send(["--no-action", "First", "First body."])
        record = silence.read_state()
        assert record is not None
        past = (datetime.now().astimezone() - timedelta(minutes=5)).isoformat()
        _ = silence.state_file().write_text(json.dumps({**record, "until": past}))
        posted: list[str] = []

        def post(_url: str, data: bytes) -> tuple[int, bytes]:
            posted.append(urllib.parse.parse_qs(data.decode())["title"][0])
            return 200, b'{"status":1}'

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(pushover.send(["--no-action", "Second", "Second body."], self.keys, post), 0)
        self.assertEqual(posted, ["First", "Second"])
        self.assertIn("silence ended at its end time; 1 kept push delivered", errors.getvalue())
        self.assertIsNone(silence.read_state())


if __name__ == "__main__":
    _ = unittest.main()
