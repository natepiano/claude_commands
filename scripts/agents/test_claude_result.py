"""Check Claude print results and extraction failures."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import override
import unittest

from scripts.agents.claude_result import claude_result
from scripts.lib.nix_env_test_support import ROOT
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class ClaudeResultTests(unittest.TestCase):
    directory: Path = Path()

    @override
    def setUp(self) -> None:
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_claude_result_extraction_preserves_error_reasons_across_empty_events(self) -> None:
        cases: tuple[tuple[list[dict[str, object]], str, str], ...] = (
            ([{"type": "result", "errors": ["credentials expired"], "result": ""}], "", "credentials expired"),
            ([{"type": "result", "is_error": True, "result": "limit reached"}], "limit reached", "limit reached"),
            ([{"type": "result", "is_error": True, "result": "weekly limit"},
              {"type": "result", "result": ""}], "", "weekly limit"),
            ([{"type": "result", "result": ""}], "", ""),
            ([{"type": "result", "result": "Pass", "subagent_stats": {"completed": 3, "failed": 1}}], "Pass", ""),
        )
        for events, report, reason in cases:
            with self.subTest(events=events):
                result = claude_result("\n".join(json.dumps(event) for event in events))
                self.assertEqual(result.report, report)
                self.assertEqual(result.reason, reason)
                self.assertEqual(result.stats, events[-1].get("subagent_stats", {}))

    def test_extractor_retains_execution_errors_when_the_output_cannot_be_written(self) -> None:
        log = self.directory / "claude.log"
        output = self.directory / "output"
        output.mkdir()
        for event, code in (({"type": "result", "result": "Pass"}, 2),
                            ({"type": "result", "errors": ["credentials expired"]}, 1)):
            with self.subTest(event=event):
                _ = log.write_text(json.dumps(event))
                result = subprocess.run([sys.executable, str(ROOT / "scripts/agents/claude_result.py"),
                                         str(log), str(output)], capture_output=True, text=True,
                                        check=False, timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(result.returncode, code)
                self.assertIn("IsADirectoryError", result.stderr)
