"""The old unit command forwards to the renamed workflow."""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from typing import cast


REPO = Path(__file__).resolve().parents[2]


class CommandStubTests(unittest.TestCase):
    def test_delegate_stub_is_a_regular_file(self) -> None:
        stub = REPO / "commands/unit/delegate.md"

        self.assertTrue(stub.is_file())
        self.assertFalse(stub.is_symlink())

    def test_delegate_stub_forwards_arguments_to_direct(self) -> None:
        text = (REPO / "commands/unit/delegate.md").read_text()

        self.assertIn("~/.claude/commands/unit/direct.md", text)
        self.assertIn("/unit:direct", text)
        self.assertIn("$ARGUMENTS", text)
        self.assertLessEqual(len(text.splitlines()), 20)

    def test_direct_command_is_a_regular_file_with_direct_usage(self) -> None:
        command = REPO / "commands/unit/direct.md"

        self.assertTrue(command.is_file())
        self.assertFalse(command.is_symlink())
        usage_lines = [
            line for line in command.read_text().splitlines()
            if line.startswith("**Usage:**")
        ]
        self.assertEqual(len(usage_lines), 1)
        self.assertTrue(usage_lines[0].startswith("**Usage:** `/unit:direct "))

    def test_hooks_reread_the_direct_command(self) -> None:
        for name in (
            "session-start-delegate-resume.py",
            "stop-delegate-continue.py",
        ):
            with self.subTest(name=name):
                text = (REPO / "scripts/hooks" / name).read_text()
                command_paths = cast(
                    list[str],
                    re.findall(r"~/\.claude/(commands/[\w/]+\.md)", text),
                )
                self.assertEqual(set(command_paths), {"commands/unit/direct.md"})


if __name__ == "__main__":
    _ = unittest.main()
