#!/usr/bin/env python3
"""Tests for pushover.py: keys, request fields, and outcomes, with no network."""

from __future__ import annotations

import io
import json
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


class PushoverTests(unittest.TestCase):
    keys: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.keys = folder / "env"
        _ = self.keys.write_text("# comment\nPUSHOVER_USER=u123\nPUSHOVER_TOKEN='a456'\n")
        _ = self.enterContext(mock.patch.object(pushover, "LOG_FILE", folder / "log"))

    def run_send(self, argv: list[str], status: int = 200, body: bytes = b'{"status":1}') -> tuple[int, dict[str, list[str]]]:
        sent: list[bytes] = []

        def post(_url: str, data: bytes) -> tuple[int, bytes]:
            sent.append(data)
            return status, body

        code = pushover.send(argv, self.keys, post)
        return code, urllib.parse.parse_qs(sent[0].decode()) if sent else {}

    def log_lines(self) -> list[dict[str, object]]:
        return [cast(dict[str, object], json.loads(line)) for line in pushover.LOG_FILE.read_text().splitlines()]

    def assert_log(self, priority: str, title: str, message: str, outcome: str) -> None:
        entries = self.log_lines()
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(set(entry), {"time", "priority", "title", "message", "outcome"})
        self.assertEqual((entry["priority"], entry["title"], entry["message"], entry["outcome"]),
                         (priority, title, message, outcome))
        stamp = datetime.fromisoformat(str(entry["time"]))
        self.assertIsNotNone(stamp.utcoffset())
        self.assertEqual(stamp.microsecond, 0)
        self.assertNotIn("u123", pushover.LOG_FILE.read_text())
        self.assertNotIn("a456", pushover.LOG_FILE.read_text())

    def test_normal_send_carries_keys_title_and_message(self) -> None:
        code, form = self.run_send(["Hana: unit", "merged"])
        self.assertEqual(code, 0)
        self.assertEqual(form["user"], ["u123"])
        self.assertEqual(form["token"], ["a456"])
        self.assertEqual(form["priority"], ["0"])
        self.assertNotIn("retry", form)

    def test_emergency_repeats_until_acknowledged(self) -> None:
        code, form = self.run_send(["--priority", "2", "Hana: blocked", "run github-warmup"], body=b'{"status":1,"receipt":"r1"}')
        self.assertEqual(code, 0)
        self.assertEqual(form["retry"], [str(pushover.RETRY_S)])
        self.assertEqual(form["expire"], [str(pushover.EXPIRE_S)])
        self.assert_log("2", "Hana: blocked", "run github-warmup", "sent, receipt r1")

    def test_missing_keys_send_nothing(self) -> None:
        _ = self.keys.write_text("PUSHOVER_USER=u123\nPUSHOVER_TOKEN=\n")
        code, form = self.run_send(["t", "m"])
        self.assertEqual((code, form), (2, {}))
        self.assert_log("0", "t", "m", "keys missing")

    def test_bad_usage_sends_nothing(self) -> None:
        for argv in (["--priority", "3", "t", "m"], ["only title"], ["t", " "]):
            self.assertEqual(self.run_send(argv), (2, {}))
        self.assertFalse(pushover.LOG_FILE.exists())

    def test_refusal_reports_pushover_errors(self) -> None:
        code, _ = self.run_send(["t", "m"], status=400, body=b'{"status":0,"errors":["application token is invalid"]}')
        self.assertEqual(code, 1)
        self.assert_log("0", "t", "m", "refused: application token is invalid")

    def test_array_reply_logs_http_refusal(self) -> None:
        code, _ = self.run_send(["t", "m"], body=b"[]")
        self.assertEqual(code, 1)
        self.assert_log("0", "t", "m", "refused: HTTP 200")

    def test_null_reply_logs_http_refusal(self) -> None:
        code, _ = self.run_send(["t", "m"], body=b"null")
        self.assertEqual(code, 1)
        self.assert_log("0", "t", "m", "refused: HTTP 200")

    def test_sent_log_keeps_multiline_text_after_limits(self) -> None:
        message = "first line\nsecond line"
        code, form = self.run_send(["Hana: unit", message])
        self.assertEqual(code, 0)
        self.assertEqual(form["message"], [message])
        self.assert_log("0", "Hana: unit", message, "sent")

    def test_overlong_title_and_message_log_the_posted_values(self) -> None:
        title = "T" * (pushover.TITLE_MAX + 3)
        message = "M" * (pushover.MESSAGE_MAX + 3)
        code, form = self.run_send([title, message])
        self.assertEqual(code, 0)
        self.assertEqual(form["title"], [title[:pushover.TITLE_MAX]])
        self.assertEqual(form["message"], [message[:pushover.MESSAGE_MAX]])
        self.assert_log("0", title[:pushover.TITLE_MAX], message[:pushover.MESSAGE_MAX], "sent")

    def test_unreachable_log_keeps_text(self) -> None:
        def unavailable(_url: str, _data: bytes) -> tuple[int, bytes]:
            raise urllib.error.URLError("offline")

        with mock.patch("sys.stderr"):
            code = pushover.send(["t", "first\nsecond"], self.keys, unavailable)
        self.assertEqual(code, 1)
        self.assert_log("0", "t", "first\nsecond", "unreachable: <urlopen error offline>")

    def test_unwritable_log_does_not_change_exit_status(self) -> None:
        folder = pushover.LOG_FILE.parent / "directory"
        folder.mkdir()
        errors = io.StringIO()
        with (mock.patch.object(pushover, "LOG_FILE", folder),
              redirect_stderr(errors)):
            code, _ = self.run_send(["t", "m"])
        self.assertEqual(code, 0)
        self.assertEqual(len(errors.getvalue().splitlines()), 1)


if __name__ == "__main__":
    _ = unittest.main()
