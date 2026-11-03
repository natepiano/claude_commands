"""Run the STE hook with session and subagent payloads."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast, override


HOOK = Path(__file__).with_name("session-start-ste.py")


class SessionStartSteTests(unittest.TestCase):
    root: Path = Path()
    hook: Path = Path()
    rule: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.hook = self.root / "scripts" / "hooks" / HOOK.name
        self.hook.parent.mkdir(parents=True)
        _ = shutil.copyfile(HOOK, self.hook)
        self.rule = self.root / "config" / "simplified-technical-english.md"
        self.rule.parent.mkdir()

    def run_hook(self, payload: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(self.hook)], input=payload, cwd=self.hook.parent,
            capture_output=True, text=True, check=False, timeout=30,
        )

    def test_both_events_load_the_full_current_rule_from_the_checkout(self) -> None:
        for event, detail in (
            ("SessionStart", {"source": "startup"}),
            ("SessionStart", {"source": "resume"}),
            ("SessionStart", {"source": "clear"}),
            ("SessionStart", {"source": "compact"}),
            ("SubagentStart", {"agent_type": "worker", "agent_id": "child"}),
        ):
            with self.subTest(event=event, detail=detail):
                context = f"# STE\n\nUse common words.\n{detail}\nKeep café accents.\n"
                _ = self.rule.write_text(context, encoding="utf-8")
                result = self.run_hook(json.dumps({"hook_event_name": event, **detail}))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")
                self.assertEqual(cast(object, json.loads(result.stdout)), {
                    "hookSpecificOutput": {"hookEventName": event, "additionalContext": context},
                })

    def test_missing_or_unreadable_rule_does_not_prevent_start(self) -> None:
        for event in ("SessionStart", "SubagentStart"):
            with self.subTest(event=event, state="missing"):
                result = self.run_hook(json.dumps({"hook_event_name": event}))
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        # A directory produces a read error even when tests run as root.
        self.rule.mkdir()
        unreadable = self.run_hook('{"hook_event_name": "SessionStart"}')
        self.assertEqual((unreadable.returncode, unreadable.stdout, unreadable.stderr), (0, "", ""))
        self.rule.rmdir()
        _ = self.rule.write_bytes(b"\xff")
        invalid_encoding = self.run_hook('{"hook_event_name": "SubagentStart"}')
        self.assertEqual((invalid_encoding.returncode, invalid_encoding.stdout, invalid_encoding.stderr), (0, "", ""))

    def test_invalid_payload_or_other_event_is_silent(self) -> None:
        _ = self.rule.write_text("Use common words.\n", encoding="utf-8")
        for payload in ("", "{", "[]", "null", "{}", '{"hook_event_name": []}',
                        '{"hook_event_name": "PostToolUse"}', "[" * 10000):
            with self.subTest(payload=payload):
                result = self.run_hook(payload)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))


if __name__ == "__main__":
    _ = unittest.main()
