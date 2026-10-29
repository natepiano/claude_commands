"""Seed real Git workspaces for the final gate and its read-only pass query."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import ClassVar, override

from scripts.direct.test_verify_untested_examples import ExampleWorkspaceFixture, VERIFY, workspace_metadata

# load 136 on natedev, 2026-10-29
TEST_HANG_GUARD_SECS = 300


class FinalPassFixture(ExampleWorkspaceFixture):
    fixture_root: ClassVar[Path]
    fixture_seed: ClassVar[Path]
    fixture_environment: ClassVar[dict[str, str]]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        builder = cls()
        builder.build_fixture()
        cls.addClassCleanup(builder.temporary.cleanup)
        cls.fixture_root = Path(builder.temporary.name)
        seed = tempfile.TemporaryDirectory(prefix="final-pass-seed-")
        cls.addClassCleanup(seed.cleanup)
        cls.fixture_seed = Path(seed.name) / "fixture"
        cls.fixture_environment = builder.environment.copy()
        _ = shutil.copytree(cls.fixture_root, cls.fixture_seed, symlinks=True)
        shutil.rmtree(cls.fixture_root)

    @override
    def setUp(self) -> None:
        _ = shutil.copytree(self.fixture_seed, self.fixture_root, symlinks=True)
        self.addCleanup(shutil.rmtree, self.fixture_root)
        self.root: Path = self.fixture_root / "repo"
        self.calls: Path = self.fixture_root / "cargo-calls"
        self.metadata_calls: Path = self.fixture_root / "metadata-calls"
        self.environment: dict[str, str] = self.fixture_environment.copy()

    @override
    def tearDown(self) -> None:
        # The restored case belongs to addCleanup; the seed lives through the class.
        pass

    def build_fixture(self) -> None:
        super().setUp()
        fixture = self.root
        git = fixture / "stubs/git"
        git.unlink()
        real_git = shutil.which("git")
        if real_git is None:
            self.fail("git is required")
        _ = git.write_text(f'#!/bin/sh\nexec "{real_git}" "$@"\n')
        git.chmod(0o755)
        self.root = fixture / "repo"
        _ = self.package()
        with (self.root / "Cargo.toml").open("a") as stream:
            _ = stream.write('\n[workspace.metadata.dev]\nfeatures = ["extra", "extra"]\n')
        self.environment.update({"PACKAGE_ROOT": str(self.root), "IN_NIX_SHELL": "1",
                                 "VERIFY_STATE_DIR": str(fixture / "verify-state"),
                                 "UNIT_SESSION_DIR": "", "NEXTEST_TEST_THREADS": "2"})
        _ = self.git("init", "--quiet")
        _ = self.git("config", "user.name", "Test Worker")
        _ = self.git("config", "user.email", "worker@example.invalid")
        _ = self.git("add", ".")
        _ = self.git("commit", "--quiet", "-m", "initial")
        # Exercise the real interpreter shim's cache, avoiding repeated version probes.
        cache = fixture / "cache/claude"
        cache.mkdir(parents=True, exist_ok=True)
        _ = (cache / "python3-3.11").write_text(sys.executable + "\n")
        _ = self.environment.pop("CLAUDE_PYTHON", None)

    def git(self, *args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=self.root, env=self.environment,
                                       text=True, stderr=subprocess.PIPE, timeout=TEST_HANG_GUARD_SECS).strip()

    def pass_files(self) -> list[Path]:
        return list((Path(self.environment["VERIFY_STATE_DIR"]) / "final_pass").glob("*.json"))

    @override
    def verify(self, *args: str) -> subprocess.CompletedProcess[str]:
        self.environment["TEST_METADATA"] = json.dumps(workspace_metadata(self.root))
        return subprocess.run(["bash", str(VERIFY), *args], cwd=self.root,
                              env=self.environment, capture_output=True, text=True,
                              check=False, timeout=TEST_HANG_GUARD_SECS)

    def assert_dirty_final(self, kind: str) -> None:
        source = self.root / "src/lib.rs"
        _ = self.git("reset", "--hard", "HEAD")
        untracked = self.root / "note"
        untracked.unlink(missing_ok=True)
        if kind == "untracked":
            _ = untracked.write_text("unsaved\n")
        else:
            _ = source.write_text("pub fn value() -> u8 { 2 }\n")
            if kind == "staged":
                _ = self.git("add", "src/lib.rs")
        result = self.verify("final")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.pass_files(), [])
        self.assertIn("no final pass record was written: the tree is dirty", result.stdout)
        answer = self.verify("final-pass")
        self.assertEqual(answer.returncode, 1, answer.stdout + answer.stderr)
        self.assertIn("the tree is dirty", answer.stderr)
