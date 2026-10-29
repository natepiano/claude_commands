"""The one herdr session rule: every Claude session is a tab of `agents`, and nothing starts another.

User, 2026-10-26: "we should not be running separate herdr sessions from this one (agents)". herdr.py
refuses a server start under any other name at run time; this check fails on code or command text
that would start one, open a session-sized workspace of its own, or send the user to one.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from typing import final

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib.code_kind import is_test_code  # noqa: E402

REPOSITORY = Path(__file__).resolve().parents[2]
LIBRARY = REPOSITORY / "scripts" / "herdr" / "herdr.py"
# Each pattern names one way to leave the shared session.
FORBIDDEN = {
    "an exclusive server start, which makes a new herdr session": re.compile(r"\bstart_new_server\("),
    "a pane in a workspace of its own, the shape of a session per Claude": re.compile(r"\bopen_pane\("),
    "an attach to a herdr session other than agents": re.compile(r"herdr session attach (?!agents\b)"),
    "a server command for a herdr session other than agents": re.compile(r"--session (?!agents\b)\S+ server\b"),
}


def _sources() -> list[Path]:
    scripts = [path for path in (REPOSITORY / "scripts").rglob("*")
               if path.suffix in (".py", ".sh") and path.is_file()
               and not is_test_code(path) and path != LIBRARY]
    return sorted([*scripts, *(REPOSITORY / "commands").rglob("*.md")])


def violations() -> list[str]:
    found: list[str] = []
    for path in _sources():
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            for what, pattern in FORBIDDEN.items():
                if pattern.search(line):
                    found.append(f"{path.relative_to(REPOSITORY)}:{number}: {what}: {line.strip()[:120]}")
    return found


@final
class OneSessionTests(unittest.TestCase):
    def test_nothing_starts_or_names_another_herdr_session(self) -> None:
        self.assertEqual(violations(), [])

    def test_each_pattern_catches_its_form(self) -> None:
        samples = {
            "an exclusive server start, which makes a new herdr session": "herdr.start_new_server(name, 90_000)",
            "a pane in a workspace of its own, the shape of a session per Claude": "pane = herdr.open_pane(name, name, cwd, {})",
            "an attach to a herdr session other than agents": "look at it with herdr session attach {slug}",
            "a server command for a herdr session other than agents": "herdr --session {name} server stop",
        }
        for what, pattern in FORBIDDEN.items():
            self.assertTrue(pattern.search(samples[what]), what)
        self.assertFalse(FORBIDDEN["an attach to a herdr session other than agents"].search(
            "herdr session attach agents, workspace hana, tab lfo"))
        self.assertFalse(FORBIDDEN["a server command for a herdr session other than agents"].search(
            "herdr --session agents server reload-agent-manifests"))


if __name__ == "__main__":
    _ = unittest.main()
