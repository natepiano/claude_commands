"""Private Git seeds and CLI invocation for assertion-guard tests."""
from __future__ import annotations

import io
import re
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar, override
from unittest.mock import patch

from scripts.direct import assertion_guard
from scripts.production.cli_fixture import invoke_cli

GUARD = Path(assertion_guard.__file__)
# load 136 on natedev, 2026-10-29
TEST_HANG_GUARD_SECS = 300

class GitRepositoryFixture(unittest.TestCase):
    root: Path = Path()
    repo: Path = Path()
    base: str = ""
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.environment = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull,
                            "GIT_CONFIG_NOSYSTEM": "1", "PYTHONDONTWRITEBYTECODE": "1"}
        _ = self.environment.pop("VERIFY_ASSERTION_BASE", None)
        _ = self.git("init", "--bare", "-q", "-b", "main", str(self.root / "remote.git"))
        _ = self.git("init", "-q", "-b", "unit")
        _ = self.git("remote", "add", "origin", str(self.root / "remote.git"))
        _ = self.git("config", "branch.unit.remote", "origin")
        _ = self.git("config", "branch.unit.merge", "refs/heads/unit")
        _ = self.save("README.md", "Fixture repository.\n")
        self.commit_base()

    def git(self, *arguments: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@t",
             "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *arguments],
            env=self.environment, capture_output=True, text=True, check=True, timeout=5,
        )
        return result.stdout.strip()

    def save(self, name: str, text: str) -> Path:
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(text, encoding="utf-8")
        return path

    def commit_base(self) -> None:
        _ = self.git("add", "--all")
        _ = self.git("commit", "-q", "--allow-empty", "-m", "fixture base")
        self.base = self.git("rev-parse", "HEAD")
        _ = self.git("push", "-q", "origin", "HEAD:refs/heads/unit")

    def guard(self, *arguments: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(GUARD), "--repo", str(self.repo), *arguments],
            env=self.environment, input=stdin, capture_output=True, text=True, check=False, timeout=5,
        )

    def change(self, path: str, before: str, after: str) -> None:
        _ = self.save(path, before)
        self.commit_base()
        _ = self.save(path, after)

    def rust(self, assertion: str, name: str = "test_value") -> str:
        return f"#[test]\nfn {name}() {{\n    {assertion}\n}}\n"

    def python(self, assertion: str, name: str = "test_value") -> str:
        return f"def {name}(self):\n    {assertion}\n"


class MergeInProgressFixture(GitRepositoryFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.save("test_merge.py", self.merge_source(incoming=True, ours=True))
        self.commit_base()
        _ = self.git("checkout", "-q", "-b", "incoming")
        _ = self.save("test_merge.py", "# incoming branch comment\n"
                      + self.merge_source(incoming=False, ours=True))
        _ = self.git("add", "test_merge.py")
        _ = self.git("commit", "-q", "-m", "incoming assertion removal")
        _ = self.git("checkout", "-q", "unit")
        _ = self.save("local.txt", "our unrelated branch change\n")
        _ = self.git("add", "local.txt")
        _ = self.git("commit", "-q", "-m", "our branch change")
        _ = self.git("merge", "-q", "--no-ff", "--no-commit", "incoming")

    def merge_source(self, *, incoming: bool, ours: bool) -> str:
        return (self.python("self.assertEqual(incoming, expected)" if incoming else "pass", "test_incoming")
                + "\n" + self.python("self.assertEqual(ours, expected)" if ours else "pass", "test_ours"))


class SeededGitRepositoryFixture(GitRepositoryFixture):
    """Restore the same private paths, including origin, before every test."""
    root: Path = Path()
    repo: Path = Path()
    base: str = ""
    environment: dict[str, str] = {}
    seed: ClassVar[Path]
    case_root: ClassVar[Path]
    case_repo: ClassVar[Path]
    case_base: ClassVar[str]
    case_environment: ClassVar[dict[str, str]]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        super().setUpClass()
        fixture = cls()
        cls.addClassCleanup(fixture.doCleanups)
        fixture.build_fixture()
        cls.case_root, cls.case_repo = fixture.root, fixture.repo
        cls.case_base = fixture.base
        cls.case_environment = fixture.environment.copy()
        cls.seed = Path(cls.enterClassContext(tempfile.TemporaryDirectory())) / "seed"
        _ = shutil.copytree(fixture.root, cls.seed)

    def build_fixture(self) -> None:
        super().setUp()

    @override
    def setUp(self) -> None:
        self.root, self.repo, self.base = self.case_root, self.case_repo, self.case_base
        self.environment = self.case_environment.copy()
        shutil.rmtree(self.root)
        _ = shutil.copytree(self.seed, self.root)

    @override
    def git(self, *arguments: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@t",
             "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *arguments],
            env=self.environment, capture_output=True, text=True, check=True,
            timeout=TEST_HANG_GUARD_SECS,
        )
        return result.stdout.strip()

    @override
    def guard(self, *arguments: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
        with patch.object(sys, "stdin", io.StringIO(stdin)):
            return invoke_cli(assertion_guard, assertion_guard.main,
                              ["--repo", str(self.repo), *arguments], self.environment, Path.cwd())

    def guard_process(self, *arguments: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(GUARD), "--repo", str(self.repo), *arguments],
            env=self.environment, input=stdin, capture_output=True, text=True, check=False,
            timeout=TEST_HANG_GUARD_SECS,
        )


class SeededMergeInProgressFixture(SeededGitRepositoryFixture, MergeInProgressFixture):
    @override
    def build_fixture(self) -> None:
        MergeInProgressFixture.setUp(self)


TILE_OLD = "assert_eq!(foot_cell.clip().bottom(), COLUMN_FOOT_TEST_AREA.bottom());"
TILE_NEW = "assert!(foot_cell.clip().bottom() <= COLUMN_FOOT_TEST_AREA.bottom());"
CLEAN = "assertion guard: no removed or weaker assertions"


class AssertionFindingFixture(SeededGitRepositoryFixture):
    def assert_clean(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), CLEAN)
        self.assertEqual(result.stderr, "")


    def assert_finding(self, result: subprocess.CompletedProcess[str], path: str, kind: str,
                       test: str = "test_value", status: int = 1) -> str:
        self.assertEqual(result.returncode, status, result.stdout + result.stderr)
        prefix = "note: " if kind == "changed" else ""
        findings = [line for line in result.stdout.splitlines() if not line.startswith("assertion guard:")]
        self.assertEqual(len(findings), 1, result.stdout)
        self.assertRegex(findings[0], rf"^{re.escape(prefix + path)}:\d+ {re.escape(test)} {kind}: ")
        self.assertEqual(result.stderr, "")
        if status == 1:
            self.assertIn("assertion guard: 1 blocking;", result.stdout.splitlines()[-1])
        return findings[0]
