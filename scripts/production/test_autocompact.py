"""The per-session auto-compact record add_unit keeps for unit directors."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import override
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import autocompact


class AutocompactTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.enterContext(patch.dict(os.environ, {"HOME": home.name}))

    def test_checked_accepts_claude_range_and_auto(self) -> None:
        for value, spelled in (("auto", "auto"), ("100k", "100k"), ("200K", "200k"), ("1m", "1M"),
                               ("1M", "1M"), ("100000", "100000"), ("1000000", "1000000")):
            with self.subTest(value=value):
                self.assertEqual(autocompact.checked(value), spelled)
        for value in ("99k", "2M", "1000001", "0", "200kb", "", "200 k", "-200k", "0200k"):
            with self.subTest(value=value), self.assertRaises(autocompact.BadAutocompact):
                _ = autocompact.checked(value)

    def test_record_then_auto_restores_the_default(self) -> None:
        self.assertEqual(autocompact.recorded("session-1"), "")
        autocompact.record("session-1", "200k")
        autocompact.record("session-2", "300K")
        self.assertEqual(autocompact.recorded("session-1"), "200k")
        autocompact.record("session-1", "auto")
        self.assertEqual(autocompact.recorded("session-1"), "")
        self.assertEqual(autocompact.recorded("session-2"), "300k")
        autocompact.record("session-1", "auto")
        self.assertEqual(list(autocompact.state_dir().iterdir()), [autocompact.state_dir() / "session-2.autocompact"])

    def test_session_ids_cannot_leave_the_state_folder(self) -> None:
        for session_id in ("../escape", "", "a/b"):
            with self.subTest(session_id=session_id), self.assertRaises(autocompact.BadAutocompact):
                autocompact.record(session_id, "200k")


if __name__ == "__main__":
    _ = unittest.main()
