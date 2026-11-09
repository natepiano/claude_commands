"""Script source evidence survives queued input until the hook consumes its hash."""
from __future__ import annotations

import os
import json
import tempfile
import time
import unittest
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

from scripts.lib import script_prompts  # noqa: E402


class ScriptPromptTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        configuration = Path(folder.name) / "config"
        configuration.mkdir()
        _ = (configuration / "script_prompts.conf").write_text("# whole: 1..\nmax_records=4096\n")
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "CONVERSATION_PAUSE_STATE_DIR": folder.name, "CONF_DIR": str(configuration),
            "NOTIFY_STATE_DIR": str(Path(folder.name) / "notify")})))

    def test_record_counts_for_its_pane_only(self) -> None:
        script_prompts.record("w1:p1", "/resume where\nyou stopped.")
        self.assertTrue(script_prompts.typed_by_script("w1:p1", "  /resume where you stopped. "))
        self.assertFalse(script_prompts.typed_by_script("w1:p2", "/resume where you stopped."))
        self.assertFalse(script_prompts.typed_by_script("w1:p1", "/resume."))
        self.assertFalse(script_prompts.typed_by_script("", "/resume where you stopped."))
        with patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p1"}):
            self.assertTrue(script_prompts.typed_here("/resume where you stopped."))

    def test_old_record_classifies_as_script_until_the_hook_consumes_it(self) -> None:
        from scripts.hooks import conversation_pause

        script_prompts.record("w1:p1", "/resume")
        old = next(path for path in script_prompts.root().iterdir() if path.name != ".lock")
        then = time.time() - 6 * 3600 - 60
        os.utime(old, (then, then))
        self.assertTrue(script_prompts.typed_by_script("w1:p1", "/resume"))
        script_prompts.record("w1:p2", "/resume")
        self.assertTrue(old.exists())
        with patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p1", "CLAUDE_CODE_SESSION_ID": ""}):
            source = conversation_pause.prompt_source("/resume", lambda: frozenset(), lambda: ())
        self.assertIs(source, conversation_pause.PromptSource.SCHEDULED)
        self.assertFalse(old.exists())
        self.assertTrue(script_prompts.typed_by_script("w1:p2", "/resume"))

    def test_full_store_removes_the_oldest_record_and_logs_its_cause(self) -> None:
        configuration = Path(os.environ["CONF_DIR"]) / "script_prompts.conf"
        _ = configuration.write_text("# whole: 1..\nmax_records=2\n")
        script_prompts.record("w1:p1", "/resume")
        oldest = next(path for path in script_prompts.root().iterdir() if path.name != ".lock")
        os.utime(oldest, (1, 1))
        script_prompts.record("w1:p2", "/resume")
        script_prompts.record("w1:p3", "/resume")
        self.assertFalse(script_prompts.typed_by_script("w1:p1", "/resume"))
        self.assertTrue(script_prompts.typed_by_script("w1:p2", "/resume"))
        self.assertTrue(script_prompts.typed_by_script("w1:p3", "/resume"))
        events = (Path(os.environ["NOTIFY_STATE_DIR"]) / "auto-silence-events.jsonl").read_text().splitlines()
        self.assertEqual(len(events), 1)
        event = cast(dict[str, object], json.loads(events[0]))
        self.assertEqual((event["event"], event["cause"], event["record"]),
                         ("script_evidence_evicted", "record_limit", oldest.name))

    def test_identical_submissions_each_classify_as_script_before_a_typed_prompt(self) -> None:
        from scripts.hooks import conversation_pause

        for attempt in range(2):
            with self.subTest(submission=attempt):
                script_prompts.record("w1:p1", "/resume")
        with patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p1", "CLAUDE_CODE_SESSION_ID": ""}):
            sources = [conversation_pause.prompt_source("/resume", lambda: frozenset(), lambda: ())
                       for _ in range(3)]
        self.assertEqual(sources, [conversation_pause.PromptSource.SCHEDULED,
                                  conversation_pause.PromptSource.SCHEDULED,
                                  conversation_pause.PromptSource.TYPED])

    def test_generated_prompt_records_both_session_and_pane(self) -> None:
        script_prompts.record("session:resumed", "/resume")
        script_prompts.record("w1:p1", "/resume")
        self.assertTrue(script_prompts.typed_by_script("session:resumed", "/resume"))
        self.assertTrue(script_prompts.typed_by_script("w1:p1", "/resume"))
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "resumed", "HERDR_PANE_ID": ""}):
            self.assertTrue(script_prompts.typed_here("/resume"))

    def test_hook_consumes_both_session_and_pane_evidence_once(self) -> None:
        script_prompts.record("session:resumed", "/resume")
        script_prompts.record("w1:p1", "/resume")
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "resumed", "HERDR_PANE_ID": "w1:p1"}):
            self.assertTrue(script_prompts.typed_here("/resume"))
            self.assertFalse(script_prompts.typed_here("/resume"))
        self.assertFalse(script_prompts.typed_by_script("session:resumed", "/resume"))
        self.assertFalse(script_prompts.typed_by_script("w1:p1", "/resume"))


if __name__ == "__main__":
    _ = unittest.main()
