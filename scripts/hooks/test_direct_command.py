"""The unit command and the hooks that re-read it."""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from typing import cast


REPO = Path(__file__).resolve().parents[2]


class CommandStubTests(unittest.TestCase):
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
            "session-start-direct-resume.py",
            "stop-direct-continue.py",
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
