"""Tests for exclude.sh: a path counts as excluded only when git then ignores it, and a refusal changes nothing."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import override

SCRIPT = Path(__file__).with_name("exclude.sh")
ALLOWLIST = "*\n!docs/\n!docs/**\n"
DOC = "docs/plans/x.md"
EXCLUDED = "EXCLUDED: Added handoff.md to .git/info/exclude\n"
REFUSED = f"Error: {DOC} is NOT excluded: .gitignore:3:!docs/** re-includes it\n"


class ExcludeTests(unittest.TestCase):
    repo: Path = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.repo = Path(self.enterContext(tempfile.TemporaryDirectory()))
        # No repository from the caller's environment, and no ignore rules from the user's own git config.
        self.environment = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
        self.environment.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        _ = self.git("init", "--quiet")

    def run_in_repo(self, *command: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(command, cwd=self.repo, env=self.environment, capture_output=True, text=True,
                              check=False)

    def git(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return self.run_in_repo("git", *arguments)

    def exclude(self, path: str) -> subprocess.CompletedProcess[str]:
        return self.run_in_repo("bash", str(SCRIPT), path)

    def ignored(self, path: str) -> bool:
        return self.git("check-ignore", "-q", "--", path).returncode == 0

    def listed(self) -> str:
        exclude_file = self.repo / ".git/info/exclude"
        return exclude_file.read_text(encoding="utf-8") if exclude_file.exists() else ""

    def track(self, path: str) -> None:
        (self.repo / path).parent.mkdir(parents=True, exist_ok=True)
        _ = (self.repo / path).write_text("text\n", encoding="utf-8")
        _ = self.git("add", "--", path)

    def allowlist(self) -> None:
        _ = (self.repo / ".gitignore").write_text(ALLOWLIST, encoding="utf-8")

    def test_an_ordinary_repo_excludes_the_path(self) -> None:
        done = self.exclude("handoff.md")
        self.assertEqual((done.returncode, done.stdout),
                         (0, EXCLUDED + "NOTE: File was not tracked by git, only added to exclude list\n"))
        self.assertTrue(self.ignored("handoff.md"))

    def test_a_second_run_on_an_excluded_path_still_succeeds(self) -> None:
        _ = self.exclude("handoff.md")
        done = self.exclude("handoff.md")
        self.assertEqual((done.returncode, done.stdout),
                         (0, "ALREADY_EXCLUDED: handoff.md is already listed in .git/info/exclude\n"))
        self.assertEqual(self.listed().count("handoff.md\n"), 1)

    def test_a_tracked_file_leaves_the_index_when_it_is_excluded(self) -> None:
        self.track("handoff.md")
        done = self.exclude("handoff.md")
        self.assertEqual((done.returncode, done.stdout),
                         (0, "UNTRACKED: Removed handoff.md from git tracking (local file kept)\n" + EXCLUDED))
        self.assertEqual(self.git("ls-files").stdout, "")
        self.assertTrue(self.ignored("handoff.md"))

    def test_a_path_the_gitignore_re_includes_is_refused_and_not_listed(self) -> None:
        self.allowlist()
        before = self.listed()
        done = self.exclude(DOC)
        self.assertEqual((done.returncode, done.stdout), (1, REFUSED))
        self.assertEqual(self.listed(), before)
        self.assertFalse(self.ignored(DOC))

    def test_a_line_already_there_stays_when_the_path_is_refused(self) -> None:
        self.allowlist()
        (self.repo / ".git/info").mkdir(exist_ok=True)
        with (self.repo / ".git/info/exclude").open("a", encoding="utf-8") as exclude_file:
            _ = exclude_file.write(f"{DOC}\n")
        before = self.listed()
        done = self.exclude(DOC)
        self.assertEqual((done.returncode, done.stdout), (1, REFUSED))
        self.assertEqual(self.listed(), before)

    def test_a_refused_tracked_file_stays_tracked(self) -> None:
        self.allowlist()
        self.track(DOC)
        done = self.exclude(DOC)
        self.assertEqual((done.returncode, done.stdout), (1, REFUSED))
        self.assertEqual(self.git("ls-files").stdout, f"{DOC}\n")
        self.assertNotIn(DOC, self.listed())

    def test_a_line_git_cannot_apply_to_the_path_is_refused_and_not_listed(self) -> None:
        before = self.listed()
        for path, reason in (("#draft.md", "no ignore rule matches it"), ("../outside.md", "git check-ignore failed")):
            done = self.exclude(path)
            self.assertEqual((done.returncode, done.stdout), (1, f"Error: {path} is NOT excluded: {reason}\n"))
            self.assertEqual(self.listed(), before)

    def test_a_pattern_anchored_to_the_repo_root_excludes_the_path(self) -> None:
        done = self.exclude("/plan.md")
        self.assertEqual((done.returncode, done.stdout),
                         (0, "EXCLUDED: Added /plan.md to .git/info/exclude\n"
                             + "NOTE: File was not tracked by git, only added to exclude list\n"))
        self.assertTrue(self.ignored("plan.md"))

    def test_an_anchored_pattern_the_gitignore_re_includes_is_refused_and_not_listed(self) -> None:
        self.allowlist()
        before = self.listed()
        done = self.exclude("/docs/x.md")
        self.assertEqual((done.returncode, done.stdout),
                         (1, "Error: /docs/x.md is NOT excluded: .gitignore:3:!docs/** re-includes it\n"))
        self.assertEqual(self.listed(), before)
        self.assertFalse(self.ignored("docs/x.md"))


if __name__ == "__main__":
    _ = unittest.main()
