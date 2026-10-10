#!/usr/bin/env python3
"""Tests for pushover.py: keys, request fields, and outcomes, with no network."""

from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
import urllib.error
import urllib.parse
from contextlib import redirect_stderr
from datetime import datetime
from pathlib import Path
from typing import cast, override
from unittest import mock

import pushover
from user_action import (FIX, ActionRefused, ActionRequired, ActionUnstated, NoActionRequired,
                         first_line, parse_user_action, refused_at)


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

    def test_only_normal_priority_may_need_no_action(self) -> None:
        none = NoActionRequired()
        required = ActionRequired("reset Codex")
        self.assertIsNone(refused_at(none, 0))
        self.assertIsInstance(refused_at(none, 1), ActionRefused)
        self.assertIsInstance(refused_at(none, 2), ActionRefused)
        for priority in (0, 1, 2):
            self.assertIsNone(refused_at(required, priority))

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

    def run_send(self, argv: list[str], status: int = 200, body: bytes = b'{"status":1}') -> tuple[int, dict[str, list[str]]]:
        sent: list[bytes] = []

        def post(_url: str, data: bytes) -> tuple[int, bytes]:
            sent.append(data)
            return status, body

        code = pushover.send(argv, self.keys, post)
        return code, urllib.parse.parse_qs(sent[0].decode()) if sent else {}

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
        self.assertEqual(form["message"], ["No action needed.\nmerged"])
        self.assertNotIn("retry", form)

    def test_emergency_repeats_until_acknowledged(self) -> None:
        code, form = self.run_send(["--priority", "2", "--action", "run github-warmup", "Hana: blocked",
                                    "GitHub credentials are cold."], body=b'{"status":1,"receipt":"r1"}')
        self.assertEqual(code, 0)
        self.assertEqual(form["retry"], [str(pushover.RETRY_S)])
        self.assertEqual(form["expire"], [str(pushover.EXPIRE_S)])
        self.assertEqual(form["message"], ["Action: run github-warmup\nGitHub credentials are cold."])
        self.assert_log("2", "Hana: blocked", "Action: run github-warmup\nGitHub credentials are cold.",
                        "sent, receipt r1")

    def test_missing_keys_send_nothing(self) -> None:
        _ = self.keys.write_text("PUSHOVER_USER=u123\nPUSHOVER_TOKEN=\n")
        code, form = self.run_send(["--no-action", "t", "m"])
        self.assertEqual((code, form), (2, {}))
        self.assert_log("0", "t", "No action needed.\nm", "keys missing")

    def test_bad_usage_sends_nothing(self) -> None:
        for argv in (["--priority", "3", "t", "m"], ["only title"], ["t", " "]):
            self.assertEqual(self.run_send(argv), (2, {}))
        self.assertFalse(pushover.LOG_FILE.exists())

    def test_refusal_reports_pushover_errors(self) -> None:
        code, _ = self.run_send(["--no-action", "t", "m"], status=400,
                                body=b'{"status":0,"errors":["application token is invalid"]}')
        self.assertEqual(code, 1)
        self.assert_log("0", "t", "No action needed.\nm", "refused: application token is invalid")

    def test_array_reply_logs_http_refusal(self) -> None:
        code, _ = self.run_send(["--no-action", "t", "m"], body=b"[]")
        self.assertEqual(code, 1)
        self.assert_log("0", "t", "No action needed.\nm", "refused: HTTP 200")

    def test_null_reply_logs_http_refusal(self) -> None:
        code, _ = self.run_send(["--no-action", "t", "m"], body=b"null")
        self.assertEqual(code, 1)
        self.assert_log("0", "t", "No action needed.\nm", "refused: HTTP 200")

    def test_sent_log_keeps_multiline_text_after_limits(self) -> None:
        message = "first line\nsecond line"
        code, form = self.run_send(["--no-action", "Hana: unit", message])
        self.assertEqual(code, 0)
        posted = "No action needed.\n" + message
        self.assertEqual(form["message"], [posted])
        self.assert_log("0", "Hana: unit", posted, "sent")

    def test_overlong_title_and_message_log_the_posted_values(self) -> None:
        title = "T" * (pushover.TITLE_MAX + 3)
        message = "M" * (pushover.MESSAGE_MAX + 3)
        code, form = self.run_send(["--no-action", title, message])
        self.assertEqual(code, 0)
        self.assertEqual(form["title"], [title[:pushover.TITLE_MAX]])
        posted = "No action needed.\n" + message[:pushover.MESSAGE_MAX - len("No action needed.\n")]
        self.assertEqual(form["message"], [posted])
        self.assert_log("0", title[:pushover.TITLE_MAX], posted, "sent")

    def test_unreachable_log_keeps_text(self) -> None:
        def unavailable(_url: str, _data: bytes) -> tuple[int, bytes]:
            raise urllib.error.URLError("offline")

        with mock.patch("sys.stderr"):
            code = pushover.send(["--no-action", "t", "first\nsecond"], self.keys, unavailable)
        self.assertEqual(code, 1)
        self.assert_log("0", "t", "No action needed.\nfirst\nsecond", "unreachable: <urlopen error offline>")

    def test_unwritable_log_does_not_change_exit_status(self) -> None:
        folder = pushover.LOG_FILE.parent / "directory"
        folder.mkdir()
        errors = io.StringIO()
        with (mock.patch.object(pushover, "LOG_FILE", folder),
              redirect_stderr(errors)):
            code, _ = self.run_send(["--no-action", "t", "m"])
        self.assertEqual(code, 0)
        self.assertEqual(len(errors.getvalue().splitlines()), 1)

    def test_unstated_action_is_sent_with_warning_and_audited(self) -> None:
        errors = io.StringIO()
        with redirect_stderr(errors):
            code, form = self.run_send(["legacy title", "legacy body"])
        self.assertEqual(code, 0)
        self.assertEqual(form["message"], ["legacy body"])
        self.assertIn("no --action or --no-action", errors.getvalue())
        self.assert_log("0", "legacy title", "legacy body", "sent without an action line")

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

if __name__ == "__main__":
    _ = unittest.main()
