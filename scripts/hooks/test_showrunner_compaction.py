#!/usr/bin/env python3
"""Tests for showrunner-compaction.py: the STATE block before a compaction and the resume text after it."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast, final, override
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

HOOK = Path(__file__).resolve().parent / "showrunner-compaction.py"
# Records each call; refuses a call with --state while REFUSE_STATE is set.
FAKE_LOG = """\
import json, os, sys
with open(os.environ["CALLS"], "a") as output:
    output.write(json.dumps(sys.argv[1:]) + "\\n")
sys.exit(1 if os.environ.get("REFUSE_STATE") and "--state" in sys.argv else 0)
"""


@final
class ShowrunnerCompactionTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.doc = Path()
        self.fake = Path()
        self.calls = Path()
        self.env: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.doc = self.root / "docs" / "example-production.md"
        self.doc.parent.mkdir()
        _ = self.doc.write_text("# Example\n", encoding="utf-8")
        timer = self.root / "notifier" / "showrunner-example"
        timer.mkdir(parents=True)
        _ = (timer / "conf").write_text(
            f"TARGET=session:showrunner-id\nCHECK=zsh /x/production_check.sh {self.doc}\n", encoding="utf-8")
        self.fake = self.root / "update_registration.py"
        _ = self.fake.write_text(FAKE_LOG, encoding="utf-8")
        self.calls = self.root / "calls.jsonl"
        self.env = {**os.environ, "NOTIFIER_STATE_DIR": str(self.root / "notifier"),
                    "SHOWRUNNER_STATE_DIR": str(self.root / "showrunner"),
                    "SHOWRUNNER_UPDATE_REGISTRATION": str(self.fake), "CALLS": str(self.calls)}

    def run_hook(self, payload: dict[str, str], **env: str) -> str:
        result = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True,
                                text=True, env={**self.env, **env}, timeout=TEST_HANG_GUARD_SECS, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def logged(self) -> list[list[str]]:
        if not self.calls.exists():
            return []
        return [cast(list[str], json.loads(line)) for line in self.calls.read_text(encoding="utf-8").splitlines()]

    def test_precompact_writes_the_state_block_from_the_state_file(self) -> None:
        state = self.root / "showrunner" / "example" / "session" / "showrunner_state.json"
        state.parent.mkdir(parents=True)
        _ = state.write_text('{"next": ["merge alpha"]}\n', encoding="utf-8")
        self.assertEqual(self.run_hook({"session_id": "showrunner-id", "hook_event_name": "PreCompact",
                                        "trigger": "auto"}), "")
        self.assertEqual(self.logged(), [["log", "before compaction (auto)", "--production", str(self.doc),
                                          "--before-compaction", "--state", str(state)]])

    def test_a_refused_state_file_still_gives_a_state_block(self) -> None:
        state = self.root / "showrunner" / "example" / "session" / "showrunner_state.json"
        state.parent.mkdir(parents=True)
        _ = state.write_text("{}\n", encoding="utf-8")
        _ = self.run_hook({"session_id": "showrunner-id", "hook_event_name": "PreCompact", "trigger": "manual"},
                          REFUSE_STATE="1")
        calls = self.logged()
        self.assertEqual(len(calls), 2)
        self.assertNotIn("--state", calls[1])

    def test_session_start_after_compaction_names_the_resume(self) -> None:
        output = self.run_hook({"session_id": "showrunner-id", "hook_event_name": "SessionStart", "source": "compact"})
        context = cast(dict[str, dict[str, str]], json.loads(output))["hookSpecificOutput"]["additionalContext"]
        self.assertIn(f"run `/showrunner:produce {self.doc} resume`", context)
        self.assertIn("just compacted", context)
        self.assertIn("`Next:` line", context)

    def test_any_other_session_or_source_gets_nothing(self) -> None:
        self.assertEqual(self.run_hook({"session_id": "unit-id", "hook_event_name": "PreCompact"}), "")
        self.assertEqual(self.run_hook({"session_id": "unit-id", "hook_event_name": "SessionStart",
                                        "source": "compact"}), "")
        self.assertEqual(self.run_hook({"session_id": "showrunner-id", "hook_event_name": "SessionStart",
                                        "source": "startup"}), "")
        self.assertEqual(self.logged(), [])


class ContextNoticeTests(unittest.TestCase):
    def test_a_showrunner_is_told_to_fill_next_and_not_to_write_a_handoff(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "post_tool_use_context_usage", Path(__file__).resolve().parent / "post-tool-use-context-usage.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        state = Path("/state/example/session/showrunner_state.json")
        notice = cast(str, module.build_context(249_000, 283_800, False, state))  # pyright: ignore[reportAny]
        self.assertIn("write no handoff doc", notice)
        self.assertIn(f"`next` list of your showrunner state file ({state})", notice)
        ordinary = cast(str, module.build_context(249_000, 283_800, False))  # pyright: ignore[reportAny]
        self.assertIn("write a handoff doc first", ordinary)


if __name__ == "__main__":
    _ = unittest.main()
