"""Check phone delivery in scans of the repository's own scripts."""
from __future__ import annotations

from pathlib import Path
import unittest

from scripts.lib.code_kind import is_test_code
from scripts.notify.test_push_texts import instruction_phone_sends, literal_delivery_calls

ROOT = Path(__file__).resolve().parents[2]
EXEMPT = {"scripts/notify/push_texts.py", "scripts/notify/pushover.py", "scripts/message/send.py"}


class PushTextTests(unittest.TestCase):
    def test_each_script_delivery_uses_the_registry(self) -> None:
        findings: list[str] = []
        for path in (ROOT / "scripts").rglob("*.py"):
            relative = path.relative_to(ROOT).as_posix()
            if relative in EXEMPT or is_test_code(path):
                continue
            findings.extend(f"{relative}:{line}: literal phone send; use push_texts.user_send_argv" for line in literal_delivery_calls(path.read_text()))
        for folder in ("commands", "codex"):
            for path in (ROOT / folder).rglob("*.md"):
                findings.extend(f"{path.relative_to(ROOT)}:{line}: unregistered phone instruction"
                                for line in instruction_phone_sends(path.read_text()))
        self.assertEqual(findings, [])

    def test_only_pushover_posts_to_pushover(self) -> None:
        """pushover.post_message adds the "Sent by" and "Template" lines; no other script may post round it."""
        found = [path.relative_to(ROOT).as_posix() for path in (ROOT / "scripts").rglob("*")
                 if path.is_file() and "__pycache__" not in path.parts and not is_test_code(path)
                 and path.name != "pushover.py" and b"api.pushover.net" in path.read_bytes()]
        self.assertEqual(found, [])
