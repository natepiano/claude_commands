"""A script prompt record holds the pane and the words, and stops counting after KEEP_SECONDS."""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import override
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib import script_prompts  # noqa: E402


class ScriptPromptTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.enterContext(patch.dict(os.environ, {"CONVERSATION_PAUSE_STATE_DIR": folder.name}))

    def test_record_counts_for_its_pane_only(self) -> None:
        script_prompts.record("w1:p1", "Continue where\nyou stopped.")
        self.assertTrue(script_prompts.typed_by_script("w1:p1", "  Continue where you stopped. "))
        self.assertFalse(script_prompts.typed_by_script("w1:p2", "Continue where you stopped."))
        self.assertFalse(script_prompts.typed_by_script("w1:p1", "Continue."))
        self.assertFalse(script_prompts.typed_by_script("", "Continue where you stopped."))
        with patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p1"}):
            self.assertTrue(script_prompts.typed_here("Continue where you stopped."))

    def test_old_record_stops_counting_and_the_next_record_removes_it(self) -> None:
        script_prompts.record("w1:p1", "go on")
        old = next(script_prompts.root().iterdir())
        then = time.time() - script_prompts.KEEP_SECONDS - 60
        os.utime(old, (then, then))
        self.assertFalse(script_prompts.typed_by_script("w1:p1", "go on"))
        script_prompts.record("w1:p2", "go on")
        self.assertFalse(old.exists())
        self.assertTrue(script_prompts.typed_by_script("w1:p2", "go on"))


if __name__ == "__main__":
    _ = unittest.main()
