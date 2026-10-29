"""The banned-word Stop hook logs each match and never blocks the reply."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import TypedDict, cast, override


# Hang guards allow for load 136 on natedev (2026-10-29).
TEST_HANG_GUARD_SECS = 300

HOOK = Path(__file__).with_name("stop-assistant-prose-banned-words.py")
SESSION = "session-1234"
# Made-up stems, so this file does not hold a word the real guide bans.
GUIDE = '---\ntier: B\n---\n## Forbidden words\n\n### "zorblax"\n\nDo not use it.\n\n### "quuxle"\n\nDo not use it.\n'


class CounterRecord(TypedDict):
    count: int
    last_triggered_at: str


class CounterFile(TypedDict):
    version: int
    words: dict[str, CounterRecord]


class StopBannedWordsHookTests(unittest.TestCase):
    home: Path = Path()
    log: Path = Path()
    counters: Path = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        guide = self.home / "rust/nate_style/rust/forbidden-words.md"
        guide.parent.mkdir(parents=True)
        _ = guide.write_text(GUIDE, encoding="utf-8")
        state = self.home / ".claude/state"
        self.log = state / "forbidden-word-prose.log"
        self.counters = state / "forbidden-word-counts.json"
        # No .claude/config/banned_words.conf exists, so hooks_enabled() reads on.
        self.environment = {**os.environ, "HOME": str(self.home)}

    def run_hook(self, message: str, **fields: object) -> subprocess.CompletedProcess[str]:
        payload = {"session_id": SESSION, "last_assistant_message": message, **fields}
        return subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                              capture_output=True, text=True, check=False,
                              env=self.environment, timeout=TEST_HANG_GUARD_SECS)

    def log_rows(self) -> list[list[str]]:
        return [line.split("\t") for line in self.log.read_text(encoding="utf-8").splitlines()]

    def counts(self) -> dict[str, int]:
        data = cast(CounterFile, json.loads(self.counters.read_text(encoding="utf-8")))
        return {stem: record["count"] for stem, record in data["words"].items()}

    def assert_silent(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_match_is_logged_and_counted_without_output(self) -> None:
        result = self.run_hook("The build\tis zorblax now.")
        self.assert_silent(result)
        rows = self.log_rows()
        self.assertEqual(len(rows), 1)
        stamp, session, stem, excerpt = rows[0]
        self.assertRegex(stamp, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$")
        self.assertEqual(session, SESSION)
        self.assertEqual(stem, "zorblax")
        self.assertEqual(excerpt, "The build is zorblax now.")
        self.assertEqual(self.counts(), {"zorblax": 1})

    def test_two_matches_on_two_lines_write_two_lines(self) -> None:
        result = self.run_hook("First zorblax line.\nSecond quuxle line, quuxle again.")
        self.assert_silent(result)
        self.assertEqual([row[2] for row in self.log_rows()], ["zorblax", "quuxle"])
        self.assertEqual(self.counts(), {"quuxle": 1, "zorblax": 1})

    def test_stop_hook_active_turn_is_still_logged(self) -> None:
        result = self.run_hook("A zorblax reply.", stop_hook_active=True)
        self.assert_silent(result)
        self.assertEqual([row[2] for row in self.log_rows()], ["zorblax"])

    def test_clean_message_writes_no_log(self) -> None:
        result = self.run_hook("Nothing to see here.")
        self.assert_silent(result)
        self.assertFalse(self.log.exists())

    def test_unwritable_state_directory_still_exits_cleanly(self) -> None:
        state = self.home / ".claude/state"
        state.parent.mkdir(parents=True)
        _ = state.write_text("not a directory", encoding="utf-8")
        result = self.run_hook("A zorblax reply.")
        self.assert_silent(result)
        self.assertEqual(result.stderr, "")

    def test_long_line_excerpt_is_cut_around_the_match(self) -> None:
        line = "lead " * 60 + "zorblax" + " tail" * 60
        result = self.run_hook(line)
        self.assert_silent(result)
        excerpt = self.log_rows()[0][3]
        self.assertLessEqual(len(excerpt), 120)
        self.assertIn("zorblax", excerpt)


if __name__ == "__main__":
    _ = unittest.main()
