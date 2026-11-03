"""Showrunner moves use the accounts command's stop-and-resume result directly."""
from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import showrunner_move
from scripts.accounts import accounts


class MainTest(unittest.TestCase):
    def test_help_teaches_only_numbered_claude_targets(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as result:
            _ = showrunner_move.main(["--help"])
        self.assertEqual(result.exception.code, 0)
        self.assertIn("<claudeN>", output.getvalue())
        self.assertNotIn("default", output.getvalue())

    def test_session_delegates_once_with_the_reason_and_preserves_the_result(self) -> None:
        with patch.object(accounts, "main", return_value=1) as move:
            code = showrunner_move.main(["session", "nightly-review", "claude 2", "--why", "weekly limit"])
        self.assertEqual(code, 1)
        move.assert_called_once_with(["move", "nightly-review", "claude2", "--why", "weekly limit"])

    def test_group_and_preview_delegate_to_the_same_accounts_command(self) -> None:
        for group in ("units", "showrunners"):
            for preview in (False, True):
                with self.subTest(group=group, preview=preview), patch.object(accounts, "main", return_value=0) as move:
                    argv = [group, "claude1"] + (["--dry-run"] if preview else [])
                    self.assertEqual(showrunner_move.main(argv), 0)
                move.assert_called_once_with(["move", *argv])
