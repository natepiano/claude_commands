"""Reject retired model names in current repository files."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[2]
OLD_NAMES = re.compile(
    "|".join((
        r"agents[.]conf(?![A-Za-z0-9_])",
        r"\bAGENTS[_]CONFIG[_]FILE\b",
        r"(?<![A-Za-z0-9])(?:test_)?agents[_]config[.]sh\b",
        r"agents[_]registry",
        r"agent[_]admin[.]sh\b",
        r"commands/agent[.]md\b",
        r"(?<![\w./-])/ag[e]nt(?![\w/-]|[.][\w/-])",
    ))
)
HISTORY_RESULT = "docs/as-built/director-model-study-results.json"
# These files record a retired path as history.
RETIRED_FILTER_HISTORY = frozenset({
    "scripts/settings/git_filters_table.sh",
    "scripts/settings/test_git_filters_retire.py",
})


class ModelNameTests(unittest.TestCase):
    def test_retired_command_matches_sentence_punctuation_but_not_other_paths(self) -> None:
        command = "/ag" + "ent"
        for text, rejected in ((f"Use {command}.", True), (f"Use {command}, then continue.", True),
                               (f"({command})", True), (command, True),
                               (f"{command}_exec", False), (f"{command}.md", False),
                               (f"scripts{command}/tool", False), ("/default_models.", False)):
            with self.subTest(text=text):
                self.assertEqual(OLD_NAMES.search(text) is not None, rejected)

    def test_current_files_use_the_model_names(self) -> None:
        files = subprocess.check_output(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=ROOT,
        ).decode().split("\0")
        problems: list[str] = []
        for name in sorted(set(files)):
            if not name or name.startswith("docs/plans/") or name == HISTORY_RESULT:
                continue
            if name in RETIRED_FILTER_HISTORY:
                continue
            path = ROOT / name
            if OLD_NAMES.search(name):
                problems.append(f"{name}: retired file name")
            if path.is_symlink() or not path.is_file():
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError:
                continue
            for number, line in enumerate(lines, 1):
                if match := OLD_NAMES.search(line):
                    problems.append(f"{name}:{number}: {match.group()}")
        self.assertEqual(problems, [], "\n".join(problems))
