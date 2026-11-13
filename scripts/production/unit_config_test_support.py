"""One private repository and home for unit configuration recovery tests."""
from __future__ import annotations

from scripts.agents import remote

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import ClassVar, override
import unittest
from unittest.mock import patch

from scripts.lib import git_exclude
from scripts.production import staff, unit_config
from scripts.production.staff_test_support import fixture_member, isolated_staff_file
from scripts.production.staff import StaffId


@dataclass(frozen=True)
class ParkedConfigBaseline:
    live: bytes
    parked: bytes


class UnitConfigFixture(unittest.TestCase):
    repository_seed: ClassVar[Path]
    parked_baselines: ClassVar[dict[unit_config.RepositoryTaploConfig | unit_config.DefaultTaploConfig, ParkedConfigBaseline]]
    root: Path = Path()
    home: Path = Path()
    worktree: Path = Path()
    source: Path = Path()
    parked: Path = Path()
    exclude: Path = Path()
    unit: StaffId = StaffId("s123456789ab")

    @classmethod
    @override
    def setUpClass(cls) -> None:
        seed_root = Path(cls.enterClassContext(tempfile.TemporaryDirectory(prefix="unit-config-seed-")))
        seed_home = seed_root / "home"
        seed_home.mkdir()
        cls.repository_seed = seed_root / "repository"
        cls.repository_seed.mkdir()
        builder = cls()
        builder.worktree = cls.repository_seed
        with patch.dict(os.environ, {
            "HOME": str(seed_home), "XDG_CONFIG_HOME": str(seed_home / ".config"),
            "GIT_CONFIG_GLOBAL": str(seed_home / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1",
        }):
            _ = builder.git("init", "-q", "--initial-branch=main")
            _ = builder.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                            "commit", "-q", "--allow-empty", "-m", "Initial commit")
        cls.parked_baselines = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="unit-config-")))
        self.home = self.root / "home"
        self.home.mkdir()
        self.enterContext(patch.dict(os.environ, {
            "HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / ".config"),
            "GIT_CONFIG_GLOBAL": str(self.home / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1",
        }))
        self.worktree = self.root / "repository"
        _ = shutil.copytree(self.repository_seed, self.worktree)
        _ = self.enterContext(isolated_staff_file(self.home / "staff.json", {}))
        owner = fixture_member("showrunner", name="configuration")
        row = fixture_member("unit", name="worker", parent=owner.id,
                             worktree=staff.PresentWorktree(remote.local_machine(), self.worktree))
        self.unit = row.id
        self.source = self.worktree / ".unit.toml"
        self.parked = unit_config.parked_path(self.unit)
        self.exclude = self.worktree / ".git/info/exclude"

    def git(self, *arguments: str) -> str:
        return subprocess.run(["git", "-C", str(self.worktree), *arguments],
                              text=True, capture_output=True, check=True).stdout.strip()

    def config(self) -> unit_config.UnitConfig:
        return unit_config.UnitConfig(unit_config.PlanFile(Path("docs/plans/worker.md")),
                                      unit_config.AppPort(8123), ("nightly-review", "build-check"))

    def retirement(self) -> unit_config.RetiredSession:
        return unit_config.RetiredSession("2026-10-31", "session-before-retire")

    def park_config(self) -> None:
        formatting = unit_config.repository_formatting(self.worktree)
        if formatting not in self.parked_baselines:
            _ = unit_config.write(self.worktree, self.config())
            _ = unit_config.park(self.unit, self.worktree, self.retirement())
            self.parked_baselines[formatting] = ParkedConfigBaseline(self.source.read_bytes(), self.parked.read_bytes())
            return
        baseline = self.parked_baselines[formatting]
        git_exclude.ensure_excluded(self.worktree, "/.unit.toml")
        for path, content in ((self.source, baseline.live), (self.parked, baseline.parked)):
            if not path.exists() or path.read_bytes() != content:
                path.parent.mkdir(parents=True, exist_ok=True)
                _ = path.write_bytes(content)

    def file_versions(self) -> dict[Path, tuple[bytes, int]]:
        return {path: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in (self.source, self.parked, self.exclude) if path.exists()}
